"""
This module presents an example of an implementation of a `grid2op.Backend` when using the powerflow
implementation "pandapower" available at `PandaPower <https://www.pandapower.org/>`_ for more details about
this backend. This file is provided as an example of a proper :class:`grid2op.Backend.Backend` implementation.

This backend currently does not work with 3 winding transformers and other exotic object.
"""

import os  # load the python os default module
import sys  # laod the python sys default module
import copy
import re
import warnings

import numpy as np
import pandas as pd

import pandapower as pp
import pandapower.networks as pn
import scipy

try:
    from .Backend import Backend
    from .Action import Action
    from .Exceptions import *
except ModuleNotFoundError:
    from Backend import Backend
    from Action import Action
    from Exceptions import *
except ImportError:
    from Backend import Backend
    from Action import Action
    from Exceptions import *

import pdb


class PandaPowerBackend(Backend):
    """
    As explained in the `grid2op.Backend` module, every module must inherit the `grid2op.Backend` class.

    This class have more attributes that are used internally for faster information retrieval.

    Attributes
    ----------
    prod_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the generators

    load_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the loads

    lines_or_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the origin end of the powerlines

    lines_ex_pu_to_kv: :class:`numpy.array`, dtype:float
        The ratio that allow the conversion from pair-unit to kv for the extremity end of the powerlines

    p_or: :class:`numpy.array`, dtype:float
        The active power flowing at the origin end of each powerline

    q_or: :class:`numpy.array`, dtype:float
        The reactive power flowing at the origin end of each powerline

    v_or: :class:`numpy.array`, dtype:float
        The voltage magnitude at the origin bus of the powerline

    a_or: :class:`numpy.array`, dtype:float
        The current flowing at the origin end of each powerline

    p_ex: :class:`numpy.array`, dtype:float
        The active power flowing at the extremity end of each powerline

    q_ex: :class:`numpy.array`, dtype:float
        The reactive power flowing at the extremity end of each powerline

    a_ex: :class:`numpy.array`, dtype:float
        The current flowing at the extremity end of each powerline

    v_ex: :class:`numpy.array`, dtype:float
        The voltage magnitude at the extremity bus of the powerline

    thermal_limit: :class:`numpy.array`, dtype:float
        The
    """
    def __init__(self):
        Backend.__init__(self)
        self.prod_pu_to_kv = None
        self.load_pu_to_kv = None
        self.lines_or_pu_to_kv = None
        self.lines_ex_pu_to_kv = None

        self.p_or = None
        self.q_or = None
        self.v_or = None
        self.a_or = None
        self.p_ex = None
        self.q_ex = None
        self.v_ex = None
        self.a_ex = None

        self._pf_init = "flat"
        self._pf_init = "results"
        self._nb_bus_before = 0

        self.thermal_limit_a = None

        self._iref_slack = -1
        self._id_bus_added = -1
        self._fact_mult_gen = -1
        self._subs_to_table = None
        self._what_object_where = None
        self._number_true_line = -1
        self._corresp_name_fun = {}
        self._get_vector_inj = {}
        self._dim_topo = -1
        self._vars_action = Action.vars_action
        self._vars_action_set = Action.vars_action_set

    def get_nb_active_bus(self):
        """
        Compute the amount of buses "in service" eg with at least a powerline connected to it.

        Returns
        -------
        res: :class:`int`
            The total number of active buses.
        """
        return np.sum(self._grid.bus["in_service"])

    def load_grid(self, path=None, filename=None):
        """
        Load the _grid, and initialize all the member of the class. Note that in order to perform topological
        modification of the substation of the underlying powergrid, some buses are added to the test case loaded. They
        are set as "out of service" unless a topological action acts on these specific substations.

        """

        if path is None and filename is None:
            raise RuntimeError("You must provide at least one of path or file to laod a powergrid.")
        if path is None:
            full_path = filename
        elif filename is None:
            full_path = path
        else:
            full_path = os.path.join(path, filename)
        if not os.path.exists(full_path):
            raise RuntimeError("There is no powergrid at \"{}\"".format(full_path))

        self._grid = pp.from_json(full_path)

        # add the slack bus that is often not modeled as a generator, but i need it for this backend to work
        bus_gen_added = None
        i_ref = None
        pp.runpp(self._grid)
        pd2ppc = self._grid._pd2ppc_lookups["bus"]  # pd2ppc[pd_id] = ppc_id
        ppc2pd = np.argsort(pd2ppc)  # ppc2pd[ppc_id] = pd_id
        for i, el in enumerate(self._grid._ppc['gen'][:, 0]):
            if int(el) not in self._grid._pd2ppc_lookups["bus"][self._grid.gen["bus"].values]:
                if bus_gen_added is not None:
                    raise RuntimeError("Impossible to recognize the powergrid")
                bus_gen_added = ppc2pd[int(el)]
                i_ref = i

        self._iref_slack = i_ref
        self._id_bus_added = self._grid.gen.shape[0]
        # see https://matpower.org/docs/ref/matpower5.0/idx_gen.html for details on the comprehension of self._grid._ppc
        pp.create_gen(self._grid, bus_gen_added,
                      p_mw=self._grid._ppc['gen'][i_ref, 1],
                      vm_pu=self._grid._ppc['gen'][i_ref, 5],
                      min_p_mw=self._grid._ppc['gen'][i_ref, 9],
                      max_p_mw=self._grid._ppc['gen'][i_ref, 8],
                      max_q_mvar=self._grid._ppc['gen'][i_ref, 3],
                      min_q_mvar=self._grid._ppc['gen'][i_ref, 4],
                      slack=True,
                      controllable=True)

        pp.runpp(self._grid)
        # this has the effect to divide by 2 the active power in the added generator, if this generator and the "slack bus"
        # one are connected to the same bus.
        # if not, it must not be done. So basically, i create a vector for which p and q for generator must be multiply
        self._fact_mult_gen = np.ones(self._grid.gen.shape[0])
        # self._fact_mult_gen[-1] += 1

        # now extract the powergrid
        self.n_lines = copy.deepcopy(self._grid.line.shape[0]) + copy.deepcopy(self._grid.trafo.shape[0])
        self.name_lines = ['{from_bus}_{to_bus}_{id_powerline_me}'.format(**row, id_powerline_me=i)
                           for i, (_, row) in enumerate(self._grid.line.iterrows())]
        transfo =  [('{hv_bus}'.format(**row), '{lv_bus}'.format(**row))
                    for i, (_, row) in enumerate(self._grid.trafo.iterrows())]
        transfo =  [sorted(el) for el in transfo]
        self.name_lines += ['{}_{}_{}'.format(*el, i+self._grid.line.shape[0]) for i, el in enumerate(transfo)]
        self.name_lines = np.array(self.name_lines)

        self.n_generators = copy.deepcopy(self._grid.gen.shape[0])
        self.name_prods = ["gen_{bus}_{index_gen}".format(**row, index_gen=i)
                           for i, (_, row) in enumerate(self._grid.gen.iterrows())]
        self.name_prods = np.array(self.name_prods)

        self.n_loads = copy.deepcopy(self._grid.load.shape[0])
        self.name_loads = ["load_{bus}_{index_gen}".format(**row, index_gen=i)
                           for i, (_, row) in enumerate(self._grid.load.iterrows())]
        self.name_loads = np.array(self.name_loads)

        self.n_substations = copy.deepcopy(self._grid.bus.shape[0])
        self.name_subs = ["sub_{}".format(i) for i, row in self._grid.bus.iterrows()]
        self.name_subs = np.array(self.name_subs)

        #  number of elements per substation
        self.subs_elements = np.zeros(self.n_substations, dtype=np.int)

        self.load_to_subid = np.zeros(self.n_loads, dtype=np.int)
        self.gen_to_subid = np.zeros(self.n_generators, dtype=np.int)
        self.lines_or_to_subid = np.zeros(self.n_lines, dtype=np.int)
        self.lines_ex_to_subid = np.zeros(self.n_lines, dtype=np.int)

        self.load_to_sub_pos = np.zeros(self.n_loads, dtype=np.int)
        self.gen_to_sub_pos = np.zeros(self.n_generators, dtype=np.int)
        self.lines_or_to_sub_pos = np.zeros(self.n_lines, dtype=np.int)
        self.lines_ex_to_sub_pos = np.zeros(self.n_lines, dtype=np.int)

        self._subs_to_table = [None for _ in range(self.n_substations)] # TODO later if I have too
        pos_already_used = np.zeros(self.n_substations, dtype=np.int)
        self._what_object_where = [[] for _ in range(self.n_substations)]

        # self._grid.line.sort_index(inplace=True)
        # self._grid.trafo.sort_index(inplace=True)
        # self._grid.gen.sort_index(inplace=True)
        # self._grid.load.sort_index(inplace=True)

        for i, (_, row) in enumerate(self._grid.line.iterrows()):
            sub_or_id = int(row["from_bus"])
            sub_ex_id = int(row["to_bus"])
            self.subs_elements[sub_or_id] += 1
            self.subs_elements[sub_ex_id] += 1
            self.lines_or_to_subid[i] = sub_or_id
            self.lines_ex_to_subid[i] = sub_ex_id

            self.lines_or_to_sub_pos[i] = pos_already_used[sub_or_id]
            pos_already_used[sub_or_id] += 1
            self.lines_ex_to_sub_pos[i] = pos_already_used[sub_ex_id]
            pos_already_used[sub_ex_id] += 1

            self._what_object_where[sub_or_id].append(("line", "from_bus", i))
            self._what_object_where[sub_ex_id].append(("line", "to_bus", i))

        lag_transfo = self._grid.line.shape[0]
        self._number_true_line = copy.deepcopy(self._grid.line.shape[0])
        for i, (_, row) in enumerate(self._grid.trafo.iterrows()):
            sub_or_id = int(row["hv_bus"])
            sub_ex_id = int(row["lv_bus"])
            self.subs_elements[sub_or_id] += 1
            self.subs_elements[sub_ex_id] += 1
            self.lines_or_to_subid[i+lag_transfo] = sub_or_id
            self.lines_ex_to_subid[i+lag_transfo] = sub_ex_id

            self.lines_or_to_sub_pos[i+lag_transfo] = pos_already_used[sub_or_id]
            pos_already_used[sub_or_id] += 1
            self.lines_ex_to_sub_pos[i+lag_transfo] = pos_already_used[sub_ex_id]
            pos_already_used[sub_ex_id] += 1

            self._what_object_where[sub_or_id].append(("trafo", "hv_bus", i))
            self._what_object_where[sub_ex_id].append(("trafo", "lv_bus", i))

        for i, (_, row) in enumerate(self._grid.gen.iterrows()):
            sub_id = int(row["bus"])
            self.subs_elements[sub_id] += 1
            self.gen_to_subid[i] = sub_id
            self.gen_to_sub_pos[i] = pos_already_used[sub_id]
            pos_already_used[sub_id] += 1

            self._what_object_where[sub_id].append(("gen", "bus", i))

        for i, (_, row)  in enumerate(self._grid.load.iterrows()):
            sub_id = int(row["bus"])
            self.subs_elements[sub_id] += 1
            self.load_to_subid[i] = sub_id
            self.load_to_sub_pos[i] = pos_already_used[sub_id]
            pos_already_used[sub_id] += 1

            self._what_object_where[sub_id].append(("load", "bus", i))

        self._compute_pos_big_topo()

        self._dim_topo = np.sum(self.subs_elements)


        # utilities for imeplementing apply_action
        self._corresp_name_fun = {}

        self._get_vector_inj = {}
        self._get_vector_inj["load_p"] = lambda grid: grid.load["p_mw"]
        self._get_vector_inj["load_q"] = lambda grid: grid.load["q_mvar"]
        self._get_vector_inj["prod_p"] = lambda grid: grid.gen["p_mw"]
        self._get_vector_inj["prod_v"] = lambda grid: grid.gen["vm_pu"]

        # "hack" to handle topological changes, for now only 2 buses per substation
        add_topo = copy.deepcopy(self._grid.bus)
        add_topo.index += add_topo.shape[0]
        add_topo["in_service"] = False
        self._grid.bus = pd.concat((self._grid.bus, add_topo))

        self.prod_pu_to_kv = self._grid.bus["vn_kv"][self._grid.gen["bus"]].values
        self.load_pu_to_kv = self._grid.bus["vn_kv"].values[self._grid.load["bus"]]
        self.lines_or_pu_to_kv  = np.concatenate((self._grid.bus["vn_kv"][self._grid.line["from_bus"].values],
                                self._grid.bus["vn_kv"][self._grid.trafo["hv_bus"].values]))
        self.lines_ex_pu_to_kv = np.concatenate((self._grid.bus["vn_kv"][self._grid.line["to_bus"].values],
                                self._grid.bus["vn_kv"][self._grid.trafo["lv_bus"].values]))
        self._nb_bus_before = self.get_nb_active_bus()

        self.thermal_limit_a = 1000 * np.concatenate((self._grid.line["max_i_ka"].values,
                                                      self._grid.trafo["sn_mva"].values / (np.sqrt(3) * self._grid.trafo["vn_hv_kv"].values)))

        self.p_or = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.q_or = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.v_or = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.a_or = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.p_ex = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.q_ex = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.v_ex = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self.a_ex = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
        self._nb_bus_before = None

    def apply_action(self, action: Action):
        """
        Specific implementation of the method to apply an action modifying a powergrid in the pandapower format.
        """

        if not isinstance(action, Action):
            raise UnrecognizedAction("Action given to PandaPowerBackend should be of class Action and not \"{}\"".format(action.__class__))

        # change the _injection if needed
        dict_injection, change_status, switch_status, set_topo_vect, switcth_topo_vect = action()
        for k in dict_injection:
            if k in self._vars_action_set:
                tmp = self._get_vector_inj[k](self._grid)
                val = dict_injection[k]
                ok_ind = np.isfinite(val)
                if k == "prod_v":
                    pass
                    # continue
                    # convert values back to pu
                    val /= self.prod_pu_to_kv # self._grid.bus["vn_kv"][self._grid.gen["bus"]].values
                    # try:
                    if np.isfinite(val[self._id_bus_added]):
                        # handling of the slack bus, where "2" generators are present.
                        pass
                        # self._grid._ppc['gen'][self._iref_slack, 5] = val[self._id_bus_added]
                        # self._grid._ppc['gen'][-1, 5] = val[self._id_bus_added]
                        self._grid["ext_grid"]["vm_pu"] = val[self._id_bus_added]
                    # ok_ind[self._id_bus_added] = False
                    # except:
                    #    pdb.set_trace()
                tmp[ok_ind] = val[ok_ind]
                # if k == "prod_v":
                #    pass
                #    pdb.set_trace()
            else:
                warn = "The key {} is not recognized by PandaPowerBackend when setting injections value.".format(k)
                warnings.warn(warn)

        # topology
        # run through all substations, find the topology. If it has changed, then update it.
        beg_ = 0
        end_ = 0
        possiblechange = set_topo_vect != 0
        if np.any(possiblechange) or np.any(switcth_topo_vect):
            actual_topo_full = self.get_topo_vect()
            if np.any(set_topo_vect[possiblechange] != actual_topo_full[possiblechange]) or np.any(switcth_topo_vect):
                for sub_id, nb_obj in enumerate(self.subs_elements):
                    nb_obj = int(nb_obj)
                    end_ += nb_obj
                    # extract all sub information
                    this_topo_set = set_topo_vect[beg_:end_]
                    this_topo_switch = switcth_topo_vect[beg_:end_]
                    actual_topo = copy.deepcopy(actual_topo_full[beg_:end_])
                    origin_topo = copy.deepcopy(actual_topo_full[beg_:end_])

                    # compute topology after action
                    if np.any(this_topo_switch):
                        # i need to switch some element
                        st = actual_topo[this_topo_switch]  # st is between 1 and 2
                        st -= 1  # st is between 0 and 1
                        st *= -1  # st is 0 or -1
                        st += 2  # st is 2 or 1 (i switched 1 <-> 2 compared to the original values)
                        actual_topo[this_topo_switch] = st
                    if np.any(this_topo_set != 0):
                        # some buses have been set
                        sel_ = this_topo_set != 0
                        actual_topo[sel_] = this_topo_set[sel_]

                    # in case the topo vector is 2,2,2 etc. i convert it back to 1,1,1 etc.
                    actual_topo = actual_topo - np.min(actual_topo) + 1
                    # implement in on the _grid
                    # change the topology in case it doesn't match the original one
                    if np.any(actual_topo != origin_topo):
                        nb_bus_before = len(np.unique(origin_topo))
                        nb_bus_now = len(np.unique(actual_topo))
                        if nb_bus_before > nb_bus_now:
                            # i must deactivate the unused bus
                            self._grid.bus["in_service"].iloc[sub_id + self.n_substations] = False
                            # print("I deactivated bus {} [before: {}; after {}]".format(sub_id,nb_bus_before,nb_bus_now))
                        elif nb_bus_before < nb_bus_now:
                            # i must activate the new bus
                            self._grid.bus["in_service"].iloc[sub_id + self.n_substations] = True
                            # print("I reactivated bus {} [before: {}; after {}]".format(sub_id,nb_bus_before,nb_bus_now))
                        # now assign the proper bus to each element
                        for i, (table, col_name, row_id) in enumerate(self._what_object_where[sub_id]):
                            self._grid[table][col_name].iloc[row_id] = sub_id if actual_topo[i] == 1 else sub_id + self.n_substations

                            #if table == "line" or table == "trafo":
                            #    # I connect it if it was disconnected
                            #    self._grid[table]["in_service"].iloc[row_id] = True
                            #    print("I reconnected a powerline")
                    beg_ += nb_obj

        # change line status if needed
        # note that it is a specification that lines status must override buses reconfiguration.
        if np.any(change_status != 0.):
            for i, el in enumerate(change_status):
                # TODO performance optim here, it can be vectorized
                if el == -1:
                    self._disconnect_line(i)
                elif el == 1:
                    self._reconnect_line(i)

        # switch line status if needed
        if np.any(switch_status):
            for i, el in enumerate(switch_status):
                # TODO performance optim here, it can be vectorized
                df = self._grid.line if i < self._number_true_line else self._grid.trafo
                tmp = i if i < self._number_true_line else i - self._number_true_line

                if el:
                    connected = df["in_service"].iloc[tmp]
                    if connected:
                        df["in_service"].iloc[tmp] = False
                    else:
                        bus_or = set_topo_vect[self.lines_or_pos_topo_vect[i]]
                        bus_ex = set_topo_vect[self.lines_ex_pos_topo_vect[i]]
                        if bus_ex == 0 or bus_or == 0:
                            raise InvalidLineStatus("Line {} was disconnected. The action switched its status, without providing buses to connect it on both ends.".format(i))
                        # reconnection has then be handled in the topology
                        df["in_service"].iloc[tmp] = True

    def _aux_get_line_info(self, colname1, colname2):
        res = np.concatenate((self._grid.res_line[colname1].values, self._grid.res_trafo[colname2].values))
        return res

    def runpf(self, is_dc=False):
        """
        Run a power flow on the underlying _grid. This implements an optimization of the powerflow
        computation: if the number of
        buses has not changed between two calls, the previous results are re used. This speeds up the computation
        in case of "do nothing" action applied.
        """
        # print("I called runpf")
        conv = True
        nb_bus = self.get_nb_active_bus()
        try:
            with warnings.catch_warnings():
                # remove the warning if _grid non connex. And it that case load flow as not converged
                warnings.filterwarnings("ignore", category=scipy.sparse.linalg.MatrixRankWarning)
                if nb_bus == self._nb_bus_before:
                    self._pf_init = "results"
                    init_vm_pu = "results"
                    init_va_degree = "results"
                else:
                    self._pf_init = "auto"
                    init_vm_pu = None
                    init_va_degree = None
                if is_dc:
                    pp.rundcpp(self._grid, check_connectivity=False)
                    self._nb_bus_before = None  # if dc i start normally next time i call an ac powerflow
                else:
                    pp.runpp(self._grid, check_connectivity=False, init=self._pf_init)
                    self._nb_bus_before = nb_bus

                if self._grid.res_gen.isnull().values.any():
                    # sometimes pandapower does not detect divergence and put Nan.
                    raise p.powerflow.LoadflowNotConverged

                # I retrieve the data once for the flows, so has to not re read multiple dataFrame
                self.p_or = self._aux_get_line_info("p_from_mw", "p_hv_mw")
                self.q_or = self._aux_get_line_info("q_from_mvar", "q_hv_mvar")
                self.v_or = self._aux_get_line_info("vm_from_pu", "vm_hv_pu")
                self.a_or = self._aux_get_line_info("i_from_ka", "i_hv_ka") * 1000
                self.a_or[~np.isfinite(self.a_or)] = 0.

                self.p_ex = self._aux_get_line_info("p_to_mw", "p_lv_mw")
                self.q_ex = self._aux_get_line_info("q_to_mvar", "q_lv_mvar")
                self.v_ex = self._aux_get_line_info("vm_to_pu", "vm_lv_pu")
                self.a_ex = self._aux_get_line_info("i_to_ka", "i_lv_ka")* 1000
                self.a_ex[~np.isfinite(self.a_ex)] = 0.

                self.v_or *= self.lines_or_pu_to_kv
                self.v_ex *= self.lines_ex_pu_to_kv

                return self._grid.converged

        except pp.powerflow.LoadflowNotConverged:
            # of the powerflow has not converged, results are Nan
            self.p_or = np.full(self.n_lines, dtype=np.float, fill_value=np.NaN)
            self.q_or = self.p_or
            self.v_or = self.p_or
            self.a_or = self.p_or
            self.p_ex = self.p_or
            self.q_ex = self.p_or
            self.v_ex = self.p_or
            self.a_ex = self.p_or
            self._nb_bus_before = None
            return False

    def copy(self):
        """
        Performs a deep copy of the power :attr:`_grid`.
        As pandapower is pure python, the deep copy operator is perfectly suited for the task.
        """
        res = copy.deepcopy(self)
        return res

    def close(self):
        """
        Called when the :class:`grid2op;Environment` has terminated, this function only reset the grid to a state
        where it has not been loaded.
        """
        del self._grid
        self._grid = None

    def save_file(self, full_path):
        """
        Save the file to json.
        :param full_path:
        :return:
        """
        pp.to_json(self._grid, full_path)

    def get_line_status(self):
        """
        As all the functions related to powerline, pandapower split them into multiple dataframe (some for transformers,
        some for 3 winding transformers etc.). We make sure to get them all here.
        """
        return np.concatenate((self._grid.line["in_service"].values, self._grid.trafo["in_service"].values)).astype(np.bool)

    def get_line_flow(self):
        """
        return the powerflow in amps in all powerlines.
        :return:
        """
        return self.a_or

    def _disconnect_line(self, id):
        if id < self._number_true_line:
            self._grid.line["in_service"].iloc[id] = False
        else:
            self._grid.trafo["in_service"].iloc[id - self._number_true_line] = False

    def _reconnect_line(self, id):
        if id < self._number_true_line:
            self._grid.line["in_service"].iloc[id] = True
        else:
            self._grid.trafo["in_service"].iloc[id - self._number_true_line] = True

    def get_topo_vect(self):
        # TODO refactor this, this takes a looong time
        res = np.full(self._dim_topo, fill_value=np.NaN, dtype=np.int)

        for i, (_, row) in enumerate(self._grid.line.iterrows()):
            bus_or_id = int(row["from_bus"])
            bus_ex_id = int(row["to_bus"])
            res[self.lines_or_pos_topo_vect[i]] = 1 if bus_or_id == self.lines_or_to_subid[i] else 2
            res[self.lines_ex_pos_topo_vect[i]] = 1 if bus_ex_id == self.lines_ex_to_subid[i] else 2

        nb = self._number_true_line
        for i, (_, row) in enumerate(self._grid.trafo.iterrows()):
            bus_or_id = int(row["hv_bus"])
            bus_ex_id = int(row["lv_bus"])

            res[self.lines_or_pos_topo_vect[i + nb]] = 1 if bus_or_id == self.lines_or_to_subid[i + nb] else 2
            res[self.lines_ex_pos_topo_vect[i + nb]] = 1 if bus_ex_id == self.lines_ex_to_subid[i + nb] else 2

        for i, (_, row) in enumerate(self._grid.gen.iterrows()):
            bus_id = int(row["bus"])
            res[self.gen_pos_topo_vect[i]] = 1 if bus_id == self.gen_to_subid[i] else 2

        for i, (_, row) in enumerate(self._grid.load.iterrows()):
            bus_id = int(row["bus"])
            res[self.load_pos_topo_vect[i]] = 1 if bus_id == self.load_to_subid[i] else 2
        return res

    def generators_info(self):
        prod_p = 1. * self._grid.res_gen["p_mw"].values
        prod_q = 1. * self._grid.res_gen["q_mvar"].values
        prod_v = self._grid.res_gen["vm_pu"].values * self.prod_pu_to_kv
        if self._grid.gen["bus"].iloc[self._id_bus_added] == self.gen_to_subid[self._id_bus_added]:
            # slack bus and added generator are on same bus. I need to add power of slack bus to this one.
            prod_p[self._id_bus_added] += self._grid._ppc["gen"][self._iref_slack, 1]
            prod_q[self._id_bus_added] += self._grid._ppc["gen"][self._iref_slack, 2]
        return prod_p, prod_q, prod_v

    def loads_info(self):
        load_p = 1. * self._grid.res_load["p_mw"].values
        load_q = 1. * self._grid.res_load["q_mvar"].values
        load_v = self._grid.res_bus["vm_pu"].values[self._grid.load["bus"]] * self.load_pu_to_kv
        return load_p, load_q, load_v

    def lines_or_info(self):
        return self.p_or, self.q_or, self.v_or, self.a_or

    def lines_ex_info(self):
        return self.p_ex, self.q_ex, self.v_ex, self.a_ex

    def shunt_info(self):
        shunt_p = copy.deepcopy(self._grid.res_shunt["p_mw"].values)
        shunt_q = copy.deepcopy(self._grid.res_shunt["q_mvar"].values)
        shunt_v = self._grid.res_bus["vm_pu"].values[self._grid.shunt["bus"]] * self._grid.bus["vn_kv"].values[self._grid.shunt["bus"]]
        shunt_bus = self._grid.shunt["bus"].values
        return shunt_p, shunt_q, shunt_v, shunt_bus

    def sub_from_bus_id(self, bus_id):
        if bus_id >= self._number_true_line:
            return bus_id - self._number_true_line
        return bus_id