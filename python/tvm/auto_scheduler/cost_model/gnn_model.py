# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name

"""Cost model based on xgboost"""
import multiprocessing
import logging
import multiprocessing.context
import multiprocessing.pool
from typing import Dict, List, Tuple
from collections import defaultdict
import time
import json

import numpy as np

import tvm.auto_scheduler
from tvm.autotvm.tuner.metric import max_curve
from .cost_model import PythonBasedModel
from ..feature import get_per_store_features_from_measure_pairs, get_per_store_features_from_states
from ..measure_record import RecordReader
# from ..search_task import SearchTask
import tvm.te as te
import tvm.tir as tir
import tvm
import networkx as nx
import matplotlib.pyplot as plt
from ...relay.expr_functor import ExprVisitor
from ..search_task import SearchTask
from ..loop_state import State
import uuid

from ..measure import MeasureInput
from ...ir.supply import GlobalVarSupply
from pyvis.network import Network
import inspect

from tqdm import tqdm

try:
    from xgboost.callback import TrainingCallback  # type: ignore
except ImportError:

    class TrainingCallback:  # type: ignore
        pass


xgb = None

logger = logging.getLogger("auto_scheduler")

def vizgraph(graph: nx.DiGraph):
    nt = Network('100%', '100%', directed=True, notebook=False)
    nt.show_buttons(filter_=['physics'])
    nt.options.physics.use_repulsion = True
    nt.from_nx(graph)
    nt.show("graph" + str(uuid.uuid4()) + ".html")
    plt.savefig("graph" + str(uuid.uuid4()) + ".png")

def node2vec():
    pass

def gnn_feature_extractor_tup(ser):
    minput: MeasureInput = MeasureInput.deserialize(ser)

    gnn_feature_extractor(minput.task, minput.state)
    
def gnn_feature_extractor(task: SearchTask, state: State):
    graph = nx.DiGraph()
    parent_stack = []
    types = []

    def preorder(node):
        current_node_id = graph.number_of_nodes()
        current_node_content = str(node)
        # Add the current node to the graph if it's not already present
        if node not in graph:
            if type(node) not in types:
                types.append(type(node))
            graph.add_node(str(current_node_id), title=current_node_content)
        # If there's a parent, add an edge from the parent to the current node
        if parent_stack:
            parent = parent_stack[-1]
            graph.add_edge(str(current_node_id), str(parent))
        # Push the current node onto the stack
        parent_stack.append(str(current_node_id))

        # Return None to continue recursion
        return None

    def postorder(node):
        # Pop the current node off the stack after processing
        if parent_stack:
            parent_stack.pop()

        # Return None to continue postorder processing
        return None

    @tvm.tir.transform.prim_func_pass(opt_level=1)
    def ast_extractor(f, mod, ctx):
        # clear the graph
        graph.clear()
        # clear the parent stack
        parent_stack.clear()
        
        # add in root node to graph and parent stack
        graph.add_node("root")
        parent_stack.append("root")
        
        tvm.tir.stmt_functor.ir_transform(f.body, preorder, postorder)
        print(graph)
        
        print("types:", len(types), types)
        return f

    # apply the state transformations
    schedule, args = task.compute_dag.apply_steps_from_state(state)
    schedule: te.Schedule

    ctx = tvm.transform.PassContext(opt_level=1, config={
        "tir.disable_vectorize": False,
        "tir.instrument_bound_checkers": False,
        "tir.add_lower_pass": [
            (0, tvm.tir.transform.InjectPrefetch()),
            (0, tvm.tir.transform.StorageFlatten(64, False)),
            (1, tvm.tir.transform.NarrowDataType(32)),
            (1, tvm.tir.transform.Simplify()),
            (1, tvm.tir.transform.VectorizeLoop(False)),
            (1, tvm.tir.transform.InjectVirtualThread()),
            (1, tvm.tir.transform.StorageRewrite()),
            (1, tvm.tir.transform.Simplify()), # skipped verifygpucode
            (1, ast_extractor)
        ]
    })
    
    with ctx:
        mod: tvm.ir.module.IRModule = tvm.lower(schedule, args)

lbb = tvm.get_global_func("auto_scheduler.local_builder.build")
schtomod = tvm.get_global_func("driver.schedule_to_module")
lowersched = tvm.get_global_func("driver.lower_schedule")


def get_gnn_features(task: SearchTask, states: List[State]):
    # Prepare MeasureInputs for all states
    # inputs = [MeasureInput(task, s).serialize() for s in states]
    inputs = [MeasureInput(task, s) for s in states]

    # Use the local_builder_build function to build in parallel
    features = lbb(inputs, 10, multiprocessing.cpu_count(), "default", 1)

    # Extract features from the build results
    # features = [extract_features_from_build_result(res) for res in build_results]  # Implement this function as needed
    return features

def get_gnn_features_sch2mod(task: SearchTask, states: List[State]):
    # Serialize the inputs for parallel processing
    serialized_inputs = [MeasureInput(task, s).serialize() for s in states]

    # Convert each serialized input to a module in parallel
    ctx = multiprocessing.get_context('fork')
    with multiprocessing.pool.Pool(multiprocessing.cpu_count(), context=ctx) as executor:
        # Use tqdm to wrap the iterable for progress tracking
        modules = list(tqdm(executor.imap(sched_to_mod, serialized_inputs), total=len(serialized_inputs)))

    # Extract features from the modules
    features = []
    for module in modules:
        # Assuming there's a function to extract features from a module
        # feature = extract_features_from_module(module)
        features.append("e")

    return features

def sched_to_mod(serialized_arg):    
    minput = MeasureInput.deserialize(serialized_arg)
    task, state = minput.task, minput.state
    schedule, args = task.compute_dag.apply_steps_from_state(state)
    # module = schtomod(schedule, args, "tmp_func", {})
    module = lowersched(schedule, args, "tmp_func", {}, False)
    return module

def get_gnn_features_old(task: SearchTask, states: List[State]):
    # parallel process all the states
    args = list(zip([(task)]*len(states), states))
    
    # make measureinputs
    inputs = [MeasureInput(task, s).serialize() for s in states]
    
    
    ctx = multiprocessing.get_context('fork')
    with multiprocessing.pool.Pool(multiprocessing.cpu_count(), context=ctx) as executor:
        features = list(executor.map(gnn_feature_extractor_tup, inputs))
    
    return features


class GNNModel(PythonBasedModel):
    """Train a GNN model that learns from the AST representation of a TIR program
    and predicts the performance of the program.

    This model takes in a state and a task, and instead of computing the features from the state, it will
    convert the state into TE, lower it to TIR, then parse the TIR to get the AST representation.

    Then we take each node in the AST, and convert it into a learned embedding.

    We then pass these embeddings into a GNN model, which will output a prediction of the performance of the program.
    """

    def __init__(
        self,
        verbose_eval=25,
        num_warmup_sample=100,
        seed=None,
        model_file=None,
        adaptive_training=False,
    ):
        global xgb
        try:
            if xgb is None:
                xgb = __import__("xgboost")
        except ImportError:
            # add "from Node" to silence
            # "During handling of the above exception, another exception occurred"
            raise ImportError(
                "XGBoost is required for XGBModel. "
                "Please install its python package first. "
                "Help: (https://xgboost.readthedocs.io/en/latest/) "
            ) from None

        self.xgb_params = {
            "max_depth": 10,
            "gamma": 0.001,
            "min_child_weight": 0,
            "eta": 0.2,
            # todo(merrymercy): automatically decrease learning rate when the loss is too large
            "n_gpus": 0,
            "nthread": multiprocessing.cpu_count() // 2,
            "verbosity": 0,
            "seed": seed or 43,
            "disable_default_eval_metric": 1,
        }
        self.bst = None
        self.plan_size = 32
        self.num_warmup_sample = num_warmup_sample
        self.verbose_eval = verbose_eval
        self.model_file = model_file
        self.adaptive_training = adaptive_training
        
        self.predictcounter = 0
        self.predictstagecounter = 0
        self.updatecounter = 0
        
        self.average_pred_time = 0
        self.average_pred_count = 0
        

        super().__init__()

        # cache measurement input/result pairs and extracted features
        self.inputs = []
        self.results = []
        self.last_train_length = 0
        self.inputs_feature_cache = []

    def update(self, inputs, results):
        self.updatecounter += len(inputs)
        print("Update ", self.updatecounter)
        
        """Update the cost model according to new measurement results (training data).
        XGBoost does not support incremental training, so we re-train a new model every time.
        Parameters
        ----------
        inputs : List[MeasureInput]
            The measurement inputs
            **** this containts
            - task
            - state
        results : List[MeasureResult]
            The measurement results
        """
        if len(inputs) <= 0:
            return
        assert len(inputs) == len(results)

        self.inputs.extend(inputs)
        self.results.extend(results)

        if (
            self.adaptive_training
            and len(self.inputs) - self.last_train_length < self.last_train_length / 5
        ):
            # Set a training threshold related to `last_train_length` to reduce the training
            # overhead when there're too many logs
            return
        self.last_train_length = len(self.inputs)

        # extract feature
        n_cached = len(self.inputs_feature_cache)
        features, normalized_throughputs, task_ids = get_per_store_features_from_measure_pairs(
            self.inputs, self.results, skip_first_n_feature_extraction=n_cached
        )
        if n_cached > 0:
            features = list(features)
            features[:n_cached] = self.inputs_feature_cache
            features = np.array(features, dtype=object)
        self.inputs_feature_cache = features
        dtrain = pack_sum_xgbmatrix(
            features, normalized_throughputs, task_ids, normalized_throughputs
        )

        # train xgb model
        self.bst = xgb.train(
            self.xgb_params,
            dtrain,
            num_boost_round=10000,
            obj=pack_sum_square_error,
            callbacks=[
                CustomCallback(
                    stopping_rounds=50,
                    metric="tr-p-rmse",
                    fevals=[pack_sum_rmse, pack_sum_average_peak_score(self.plan_size)],
                    evals=[(dtrain, "tr")],
                    maximize=False,
                    verbose_eval=self.verbose_eval,
                )
            ],
        )

        # Update the model file if it has been set
        if self.model_file:
            self.save(self.model_file)

    def predict(self, task, states):
        self.predictcounter += len(states)
        print("Predict ", self.predictcounter)

        """Predict the scores of states
        Parameters
        ----------
        search_task : SearchTask
            The search task of states
        statse : List[State]
            The input states
        Returns
        -------
        scores: List[float]
            The predicted scores for all states
        """
        
        # start_time_sch2mod = time.time()
        # features = get_gnn_features_sch2mod(task, states)
        # end_time_sch2mod = time.time()
        # print(f"Time taken for get_gnn_features_sch2mod: {end_time_sch2mod - start_time_sch2mod:.6f} seconds")        


        # # Timing the first function call
        # start_time_gnn = time.time()
        # features = get_gnn_features(task, states)
        # end_time_gnn = time.time()
        # print(f"Time taken for get_gnn_features: {end_time_gnn - start_time_gnn:.6f} seconds")

        # start_time_gnn_old = time.time()
        # features = get_gnn_features_old(task, states[:1])
        # end_time_gnn_old = time.time()
        # print(f"Time taken for get_gnn_features_old: {end_time_gnn_old - start_time_gnn_old:.6f} seconds")


        # Timing the second function call
        start_time_per_store = time.time()
        features = get_per_store_features_from_states(states, task)
        end_time_per_store = time.time()
        print(f"Time taken for get_per_store_features_from_states: {end_time_per_store - start_time_per_store:.6f} seconds")
        
        print("model?", self.bst is not None and len(self.inputs) > self.num_warmup_sample)
        if self.bst is not None and len(self.inputs) > self.num_warmup_sample:
            dtest, pack_ids = feature_to_pack_sum_xgbmatrix(features)
            raw_preds = self.bst.predict(dtest)
            ret = predict_throughput_pack_sum(raw_preds, pack_ids)
        else:
            ret = np.random.uniform(0, 1, (len(states),))

        # Predict -inf for invalid states that failed to be lowered.
        for idx, feature in enumerate(features):
            if feature.min() == feature.max() == 0:
                ret[idx] = float("-inf")

        return ret

    def predict_stages(self, task, states):
        self.predictstagecounter += len(states)
        print("Predict stage ", self.predictstagecounter)

        
        """Predict the scores of all stages in states. This is the breakdown version of `predict`.

        Parameters
        ----------
        search_task : SearchTask
            The search task of states
        statse : List[State]
            The input states

        Returns
        -------
        scores: List[float]
            The predicted scores for all stages in all states in the packed format

        Note
        ----
        For faster data copy between c++ and python, the python part returns scores in a
        single flatten array using a packed format. The c++ part then unpacks the flatten array.
        The packed format is:
        {

          float  scores[N];                 // scores[i] is the score for states[i].
          int    n_stage_0;                 // the number of stages in states[0]
          float  stage_scores_0[[n_stage_0] // the scores for all stages in states[0]
          int    n_stage_1;                 // the number of stages in states[1]
          float  stage_scores_1[n_stage_1]; // the scores for all stages in states[1]
          ...
          int    n_stage_i;                 // the number of stages in states[i]
          float  stage_scores_1[n_stage_i]; // the scores for all stages in states[i]
          ...  // untill i == N - 1

        }
        To implement this format, we also store int as float, so we can store all numbers
        into a single float array.
        """
        features = get_per_store_features_from_states(states, task)
        if self.bst is not None and len(self.inputs) > self.num_warmup_sample:
            dtest, pack_ids = feature_to_pack_sum_xgbmatrix(features)
            raw_preds = self.bst.predict(dtest)
            breakdown = predict_throughput_pack_sum(raw_preds, pack_ids)
            stage_scores = [[] for _ in range(len(states))]
            for pred, pack_id in zip(raw_preds, pack_ids):
                stage_scores[pack_id].append(pred)
            for idx, stage_score in enumerate(stage_scores):
                breakdown = np.append(breakdown, len(stage_score))
                breakdown = np.concatenate((breakdown, np.array(stage_score)))
        else:
            breakdown = np.concatenate(
                (np.random.uniform(0, 1, (len(states),)), np.zeros(len(states)))
            )

        # Predict 0 for invalid states that failed to be lowered.
        for idx, feature in enumerate(features):
            if feature.min() == feature.max() == 0:
                breakdown[idx] = float("-inf")

        return breakdown

    def update_from_file(self, file_name, n_lines=None):
        """Load measure records from a log file to update the cost model.
        This function can be used to pre-train the cost model with history log files.
        Parameters
        ----------
        file_name: str
            The filename
        n_lines: Optional[int]
            Only load first n lines of the log file
        """
        inputs, results = RecordReader(file_name).read_lines(n_lines)
        logger.info("XGBModel: Loaded %s measurement records from %s", len(inputs), file_name)
        self.update(inputs, results)

    def save(self, file_name: str):
        """Save the model to a file
        Parameters
        ----------
        file_name: str
            The filename
        """
        self.bst.save_model(file_name)

    def load(self, file_name: str):
        """Load the model from a file
        Parameters
        ----------
        file_name: str
            The filename
        """
        if self.bst is None:
            self.bst = xgb.Booster(self.xgb_params)
        self.bst.load_model(file_name)
        self.num_warmup_sample = -1


def feature_to_pack_sum_xgbmatrix(xs):
    """Convert an extracted multi-stage feature vector to a xgbmatrx in pack-sum format
    Parameters
    ----------
    xs: np.ndarray
        The feature vector
    Returns
    -------
    dmatrix: xgb.DMatrix
        The DMatrix
    pack_ids: List[int]
        pack ids information
    """
    x_flatten = []
    pack_ids = []

    for ct, x in enumerate(xs):
        for row in x:
            x_flatten.append(row)
            pack_ids.append(ct)

    return xgb.DMatrix(np.array(x_flatten)), pack_ids


def pack_sum_xgbmatrix(xs, ys, gids=None, weights=None):
    """Convert (feature, label) pairs into a xgb matrix with pack-sum format
    Parameters
    ----------
    xs: np.ndarray
        The feature vector
    ys: np.ndarray
        The normaizlied throughput
    gids: Optional[List[int]]
        Group id (task id)
    weights: Optional[np.ndarray]
        The weight of samples
    Returns
    -------
    dmatrix: xgb.DMatrix
        The DMatrix with pack-sum information
    """
    if gids is not None:
        # sort by group
        indices = gids.argsort()
        xs, ys = xs[indices], ys[indices]
        group_sizes = np.bincount(gids)
        if weights is not None:
            weights = weights[indices]
    else:
        # assume it has only one group
        group_sizes = [len(xs)]

    x_flatten = []
    y_flatten = []
    weights_flatten = []
    pack_ids = []

    if weights is not None:
        for ct, (x, y, w) in enumerate(zip(xs, ys, weights)):
            for row in x:
                x_flatten.append(row)
                y_flatten.append(y)
                weights_flatten.append(w)
                pack_ids.append(ct)
    else:
        for ct, (x, y) in enumerate(zip(xs, ys)):
            for row in x:
                x_flatten.append(row)
                y_flatten.append(y)
                pack_ids.append(ct)

    ret = xgb.DMatrix(np.array(x_flatten), y_flatten)
    if weights is not None:
        ret.set_weight(weights_flatten)
    dmatrix_context.set("pack_ids", ret, np.array(pack_ids))
    dmatrix_context.set("group_sizes", ret, group_sizes)
    return ret


def predict_throughput_pack_sum(raw_preds, pack_ids):
    """Predict the throughputs for predictions in pack-sum format
    Parameters
    ----------
    raw_preds: np.ndarray
        The raw predictions
    pack_ids: List[int]
        The pack id for predictions
    Returns
    -------
    throughputs: np.ndarray
        The throughput
    """
    sum_pred = np.bincount(pack_ids, weights=raw_preds)
    return sum_pred


def pack_sum_square_error(preds, dtrain):
    """Implement square error loss on pack-sum format as
     a custom objective function for xgboost.
    Parameters
    ----------
    preds: np.ndarray
        The predicitons
    dtrain: xgb.DMatrix
        The training set
    Returns
    -------
    gradient: np.ndarray
    hessian: np.ndarray
        gradient and hessian according to the xgboost format
    """
    pack_ids = dmatrix_context.get("pack_ids", dtrain)
    weight = dtrain.get_weight()

    sum_pred = np.bincount(pack_ids, weights=preds)
    x = sum_pred[pack_ids]
    y = dtrain.get_label()
    gradient = x - y
    hessian = np.ones_like(gradient)

    if len(weight) == 0:
        return gradient, hessian

    return gradient * weight, hessian * weight


def pack_sum_rmse(raw_preds, labels):
    """Evaluate RMSE (rooted mean square error) in the pack-sum format
    Parameters
    ----------
    raw_preds: np.ndarray
        The raw prediction
    labels: xgb.DMatrix
        The groud-truth label matrix
    Returns
    -------
    name: str
    score: float
        The name and score of this metric
    """
    pack_ids = dmatrix_context.get("pack_ids", labels)
    preds = predict_throughput_pack_sum(raw_preds, pack_ids)[pack_ids]
    return "p-rmse", np.sqrt(np.mean(np.square((preds - labels.get_label()))))


def pack_sum_average_peak_score(N):
    """Return the evaluation function for average-peak-score@N
    Parameters
    ----------
    N: int
        The "N" in "average-peak-score@N"
    Returns
    -------
    The evaluation function
    """

    def feval(preds, labels):
        """Evaluate average-peak-score@N in the pack-sum format
        Parameters
        ----------
        raw_preds: np.ndarray
            The raw prediction
        labels: xgb.DMatrix
            The groud-truth label matrix
        Returns
        -------
        name: str
        score: float
        The name and score of this metric
        """
        group_sizes = dmatrix_context.get("group_sizes", labels, [len(preds)])
        pack_ids = dmatrix_context.get("pack_ids", labels)

        preds = predict_throughput_pack_sum(preds, pack_ids)
        labels = (
            np.bincount(pack_ids, weights=labels.get_label())
            / np.unique(pack_ids, return_counts=True)[1]
        )

        scores = []
        offset = 0
        for size in group_sizes:
            preds_group = preds[offset : offset + size]
            labels_group = labels[offset : offset + size]
            offset += size

            trials = np.argsort(preds_group)[::-1][:N]
            trial_scores = labels_group[trials]
            curve = max_curve(trial_scores) / np.max(labels_group)
            scores.append(np.mean(curve))
        return f"a-peak@{N}", np.mean(scores)

    return feval


class XGBoostCallback(TrainingCallback):
    """Base class for XGBoost callbacks."""

    def __call__(self, env: "xgb.core.CallbackEnv"):
        # Compatibility with xgboost < 1.3
        return self.after_iteration(env.model, env.iteration, env.evaluation_result_list)

    def after_iteration(self, model: "xgb.Booster", epoch: int, evals_log: Dict):
        raise NotImplementedError


class CustomCallback(XGBoostCallback):
    """
    Callback function for xgboost.
    Support custom evaluation function and early-stopping.
    """

    def __init__(
        self,
        stopping_rounds,
        metric,
        fevals,
        evals=(),
        log_file=None,
        maximize=False,
        verbose_eval=True,
        skip_every=2,
    ):
        """Init function"""
        self.stopping_rounds = stopping_rounds
        self.metric = metric
        self.metric_shortname = metric.split("-")[1]
        self.fevals = fevals
        self.evals = evals
        self.log_file = log_file
        self.maximize = maximize
        self.verbose_eval = verbose_eval
        self.skip_every = skip_every
        self.state = {}

    def after_iteration(self, model: "xgb.Booster", epoch: int, evals_log: Dict):
        """Run after each iteration.  Return True when training should stop."""
        # pylint:disable = import-outside-toplevel
        try:
            from xgboost.callback import _fmt_metric  # type: ignore
        except ImportError:
            # Compatibility with xgboost >= 1.6
            def _fmt_metric(value, show_stdv=True):
                """format metric string"""
                if len(value) == 2:
                    return f"{value[0]}:{value[1]:.5f}"
                if len(value) == 3:
                    if show_stdv:
                        return f"{value[0]}:{value[1]:.5f}+{value[2]:.5f}"
                    return f"{value[0]}:{value[1]:.5f}"
                raise ValueError("wrong metric value", value)

        ##### init state #####
        if not self.state:
            self.state["maximize_score"] = self.maximize
            self.state["best_iteration"] = 0
            if self.maximize:
                self.state["best_score"] = float("-inf")
            else:
                self.state["best_score"] = float("inf")

            assert model is not None
            if model.attr("best_score") is not None:
                self.state["best_score"] = float(model.attr("best_score"))
                self.state["best_iteration"] = int(model.attr("best_iteration"))
                self.state["best_msg"] = model.attr("best_msg")
            else:
                model.set_attr(best_iteration=str(self.state["best_iteration"]))
                model.set_attr(best_score=str(self.state["best_score"]))
        res_dict = {}

        if epoch % self.skip_every == 1:
            return False

        ##### evaluation #####
        for feval in self.fevals:
            bst_eval = model.eval_set(self.evals, epoch, feval)
            res = [x.split(":") for x in bst_eval.split()]
            for kv in res[1:]:
                res_dict[kv[0]] = [float(kv[1])]

        eval_res = []
        keys = list(res_dict.keys())
        keys.sort(key=lambda x: x if self.metric_shortname not in x else "a" + x)
        for key in keys:
            v = res_dict[key]
            eval_res.append([key] + v)

        ##### print eval result #####
        if (
            not isinstance(self.verbose_eval, bool)
            and self.verbose_eval
            and epoch % self.verbose_eval == 0
        ):
            infos = [f"XGB iter: {epoch:3d}"]
            for item in eval_res:
                if "null" in item[0]:
                    continue
                infos.append(f"{item[0]}: {item[1]:.6f}")

            logger.debug("\t".join(infos))
            if self.log_file:
                with open(self.log_file, "a") as fout:
                    fout.write("\t".join(infos) + "\n")

        ##### choose score and do early stopping #####
        score = None
        for item in eval_res:
            if item[0] == self.metric:
                score = item[1]
                break
        assert score is not None

        best_score = self.state["best_score"]
        best_iteration = self.state["best_iteration"]
        maximize_score = self.state["maximize_score"]

        if (maximize_score and score > best_score) or (not maximize_score and score < best_score):
            msg = f"[{epoch}] " + "\t".join([_fmt_metric(x) for x in eval_res])
            self.state["best_msg"] = msg
            self.state["best_score"] = score
            self.state["best_iteration"] = epoch
            # save the property to attributes, so they will occur in checkpoint.
            if model is not None:
                model.set_attr(
                    best_score=str(self.state["best_score"]),
                    best_iteration=str(self.state["best_iteration"]),
                    best_msg=self.state["best_msg"],
                )
        elif epoch - best_iteration >= self.stopping_rounds:
            best_msg = self.state["best_msg"]
            if self.verbose_eval:
                logger.debug("XGB stopped. Best iteration: %s ", best_msg)
            return True

        return False
