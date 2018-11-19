# *****************************************************************
#
# Licensed Materials - Property of IBM
#
# (C) Copyright IBM Corp. 2018. All Rights Reserved.
#
# US Government Users Restricted Rights - Use, duplication or
# disclosure restricted by GSA ADP Schedule Contract with IBM Corp.
#
# *****************************************************************

"""Simulator
"""

import os

import tensorflow as tf
from tensorflow.contrib.memory_stats.python.ops import memory_stats_ops

from tensorflow_large_model_support import util as ut


class Simulator(object):
    """A simulator to simulate how LMS works.
    It is used to predict whether LMS works with given LMS parameters.
    """
    def __init__(self, lms, ratio=0.9, swapout_delay=1, debug_level=1,
                 plot=False):
        self._lms = lms
        self._ratio = ratio
        self._debug_level = debug_level
        self._plot = plot
        # swapout ops would take time to transfer data from device to host.
        # this variable is used to simulate how slow the transfer is.
        # TODO: this variable should depend on the tensor size.
        self._swapout_delay = swapout_delay
        # memory to store tensors
        self._mem = {}
        # used memory at a given time
        self._used_mem = 0
        # memory limitation
        self._max_mem = 0
        # traces of used memory
        self._mem_traces = []
        # each tensor has a ref_count showing how many ops will consume it
        self._ref_counts = {}

        # aliases
        self._graph = self._lms._graph
        self._topo_sort = self._lms._topo_sort
        self._lms_dir = self._lms._lms_dir
        self._excl_src_ops = self._lms._excl_src_ops
        self._excl_dest_ops = self._lms._excl_dest_ops
        self._distance = self._lms._distance
        self._get_level = self._lms._get_level
        self._get_ops_by_level = self._lms._get_ops_by_level
        self._get_earliest_op = self._lms._get_earliest_op
        self._groupby = self._lms._groupby
        self._batch_size = self._lms._batch_size

        self._initialize()

    def _initialize(self):
        """Initialize some variables
        """
        with tf.Session() as sess:
            self._max_mem = sess.run(memory_stats_ops.BytesLimit())
        # exclude memories for variables
        for op in self._graph.get_operations():
            if 'Variable' in op.type:
                if len(op.outputs) > 0:
                    ts = op.outputs[0]
                    var_size = self._ts_size(ts)
                    self._max_mem -= var_size
        # use only `ratio` percent of the available memory
        self._max_mem *= self._ratio
        self._log_info("Available memory for simulation: {}".format(
            self._max_mem), 0)

    def _reset(self):
        """Reset memory to the initial state.
        """
        self._mem = {}
        self._ref_counts = {}
        self._used_mem = 0
        self._mem_traces = []

    def play(self, threshold, ahead, groupby):
        """Check whether LMS works with parameters `threshold` and `ahead`.

        Return:
          True if successfully. Otherwise, False.
        """

        self._reset()
        self._log_info("Simulating for threshold {}, ahead {}, groupby {}".format(
            threshold, ahead, groupby), 0)

        # keep tensors that were swapped output
        swapouts = set()

        # simulate swapin operations.
        # store tensors which are swapped in at a given level.
        # when a tensor is swapped in, it is added to mem and its `ref_count`
        # increases by one.
        swapins = {}

        # start simulating
        passed = True
        for k in range(0, self._topo_sort.size):
            self._log_info("Simulate level {}".format(k))

            self._gc()  # collect swapout tensors

            k_ops = self._get_ops_by_level(k)
            # allocate memory for swapin tensors
            name_size_lifetimes = swapins[k] if k in swapins else set()
            for nsl in name_size_lifetimes:
                ts_name, ts_size, lifetime = nsl
                self._log_info("[{}] swapped in {}".format(k, ts_name))
                ok = self._allocate(ts_name, ts_size, lifetime)
                if not ok:
                    passed = False
                    if not self._plot:
                        return passed

            for op in k_ops:
                self._log_info("[{}] execute op {}".format(k, op.name))
                in_tensors = set(op.inputs)
                out_tensors = set(op.outputs)
                op_name = op.name
                # allocate memory for inputs
                for ts in in_tensors:
                    ts_name = ts.name
                    # whether this tensor was swapped in?
                    found, _ = self._is_swapin_tensor(ts_name, op_name)
                    if found:
                        continue
                    else:
                        if ts_name not in self._mem:
                            ok = self._allocate(
                                ts_name, ts_size, len(ts.consumers()))
                            if not ok:
                                passed = False
                                if not self._plot:
                                    return passed

                # allocate memory for outputs
                for ts in out_tensors:
                    ts_name = ts.name
                    n_consumers = len(ts.consumers())
                    if n_consumers == 0:
                        continue    # no ops consuming `ts`
                    ok = self._allocate(ts_name, self._ts_size(ts), n_consumers)
                    if not ok:
                        passed = False
                        if not self._plot:
                            return passed

                # simulate execution
                for ts in in_tensors:
                    # check if the tensor is swapped in
                    ts_name = ts.name
                    _, s = self._is_swapin_tensor(ts_name, op_name)
                    self._ref_counts[s] -= 1

                # update ref_counts for swapout tensors
                for ts in {x for x in swapouts
                           if x in self._mem and self._ref_counts[x] > 0}:
                    self._ref_counts[ts] -= 1

                # garbage collection
                self._gc()  # collect input and swapout tensors

                # swap out tensors
                if op in self._excl_src_ops:
                    continue
                for ts in out_tensors:
                    ndims = ts.shape.ndims
                    if ndims is None or ndims <= 1:
                        continue
                    ts_name = ts.name
                    ts_size = self._ts_size(ts)
                    dest_ops = {dest
                                for dest in ts.consumers()
                                if self._distance(op, dest) > threshold}
                    dest_ops -= self._excl_dest_ops
                    if not dest_ops:
                        continue
                    # swapout tensors will be collected later
                    self._ref_counts[ts_name] -= len(dest_ops)
                    self._ref_counts[ts_name] += self._swapout_delay
                    swapouts.add(ts_name)
                    self._log_info("[{}] swapped out {}".format(k, ts_name))
                    # add swapin ops
                    dests_grp = self._groupby(dest_ops, groupby)
                    for dests in dests_grp:
                        # create a new tensor to simulate swapin
                        s = ts_name + "_" + "_".join(x.name for x in dests)
                        ts_info = (s, ts_size, len(dests))
                        # put the tensor into the swapins queue
                        dest = self._get_earliest_op(dests)
                        sin_level = self._get_level(dest) - ahead
                        if sin_level in swapins:
                            swapins[sin_level] |= {ts_info}
                        else:
                            swapins[sin_level] = {ts_info}

            self._log_info("[{}] available memory {}".format(
                k, self._get_free_mem))

        if self._plot:
            self._generate_diagram(threshold, ahead, groupby)
        self._log_info("Swapped out {} tensors".format(len(swapouts)))
        return passed

    def _allocate(self, ts_name, ts_size, lifetime):
        """Allocate memory for tensor `ts`.

        Return:
          True if successfully. Otherwise, False.
        """
        succeed = True
        if ts_size >= self._get_free_mem():
            succeed = False
            # out of memory
            self._log_info(
                "OOM tensor {}, size {}, used {}, free {}".format(
                    ts_name, ts_size,
                    self._used_mem,
                    self._get_free_mem()))
            if not self._plot:
                return succeed
        # allocate
        self._used_mem += ts_size
        self._mem[ts_name] = ts_size
        self._ref_counts[ts_name] = lifetime
        self._mem_traces.append(self._used_mem)
        self._log_info(
            "allocated {} bytes for {}, used {}, free {}".format(
                ts_size, ts_name,
                self._used_mem,
                self._get_free_mem()))
        return succeed

    def _release(self, ts_name):
        """Release memory taken by tensor `ts`.
        """
        ts_size = self._mem[ts_name]
        del self._mem[ts_name]
        del self._ref_counts[ts_name]
        self._used_mem -= ts_size
        self._mem_traces.append(self._used_mem)
        self._log_info(
            "released {} bytes taken by {}, used {}, free {}".format(
                ts_size, ts_name,
                self._used_mem,
                self._get_free_mem()))

    def _gc(self):
        """Simulate TensorFlow garbage collection.
        A tensor will be released from mem if its `ref_count` becomes zero.
        """
        cands = [k for k, v in self._ref_counts.items() if v == 0]
        for ts_name in cands:
            self._release(ts_name)

    def _get_free_mem(self):
        """Return the available memory
        """
        return self._max_mem - self._used_mem

    def _generate_diagram(self, threshold, ahead, groupby):
        """Generate a diagram of memory consumption.
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.plot([m/(1e9) for m in self._mem_traces],
                 label="LMS(swapout_threshold: {}".format(threshold) +
                 ", swapin_ahead: {}".format(ahead) +
                 ", swapin_groupby: {})".format(groupby))
        plt.title("Simulation of memory consumption")
        plt.xlabel("Allocation/Deallocation steps")
        plt.ylabel("GigaBytes")
        plt.legend(loc='upper left', fontsize='x-small')
        plt.grid(True)
        if not os.path.exists(self._lms_dir):
            os.makedirs(self._lms_dir)
        plt.savefig("{}/tflms_simulator_mem_traces".format(self._lms_dir) +
                    "_swapout_threshold{}".format(threshold) +
                    "_swapin_ahead{}".format(ahead) +
                    "_swapin_groupby{}".format(groupby) +
                    ".pdf",
                    format='pdf')
        plt.close()

    def _ts_size(self, ts):
        return ut.get_tensor_size(ts, self._batch_size)


    def _is_swapin_tensor(self, ts_name, op_name):
        """Check if the tensor is a swapin tensor or not.
        """
        for s in self._mem:
            if (ts_name in s) and (op_name in s):
                return (True, s)
        return (False, ts_name)

    def _log_info(self, msg, level=-1, offset=0):
        if level >= 0:
            self._lms._log_info("[Simulator] " + msg, level, offset)
        else:
            self._lms._log_info(
                "[Simulator] " + msg, self._debug_level, offset)