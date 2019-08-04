# https://github.com/ray-project/ray/blob/master/python/ray/rllib/examples/multiagent_cartpole.py
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
"""Simple example of setting up a multi-agent policy mapping.
Control the number of agents and policies via --num-agents and --num-policies.
This works with hundreds of agents and policies, but note that initializing
many TF policies will take some time.
Also, TF evals might slow down with large numbers of policies. To debug TF
execution, set the TF_TIMELINE_DIR environment variable.
"""
import os
os.environ['RAY_DEBUG_DISABLE_MEMORY_MONITOR'] = "True"

import argparse
import gym
import random
import numpy as np

import ray
from ray import tune
from ray.rllib.models import Model, ModelCatalog
from ray.rllib.tests.test_multi_agent_env import MultiCartpole
from ray.tune.registry import register_env
from ray.rllib.utils import try_import_tf



import unittest

from ray.rllib.agents.pg import PGTrainer
from ray.rllib.agents.pg.pg_policy import PGTFPolicy
from ray.rllib.agents.dqn.dqn_policy import DQNTFPolicy
from ray.rllib.optimizers import (SyncSamplesOptimizer, SyncReplayOptimizer,
                                  AsyncGradientsOptimizer)
from ray.rllib.tests.test_rollout_worker import (MockEnv, MockEnv2, MockPolicy)
from ray.rllib.evaluation.rollout_worker import RolloutWorker
from ray.rllib.policy.policy import Policy
from ray.rllib.evaluation.metrics import collect_metrics
from ray.rllib.evaluation.worker_set import WorkerSet
from ray.rllib.env.base_env import _MultiAgentEnvToBaseEnv
from ray.rllib.env.multi_agent_env import MultiAgentEnv



import sys
if "../" not in sys.path:
    sys.path.append("../")
#from exchg.x.y import z
from exchg.exchg import Exchg

tf = try_import_tf()

parser = argparse.ArgumentParser()
parser.add_argument("--num-agents", type=int, default=4)
#parser.add_argument("--num-policies", type=int, default=2)
parser.add_argument("--num-policies", type=int, default=1)
parser.add_argument("--num-iters", type=int, default=3)
parser.add_argument("--simple", action="store_true")

class CustomModel1(Model):
    def _lstm(self, Inputs, cell_size):
        s = tf.expand_dims(Inputs, axis=1, name='time_major')  # [time_step, feature] => [time_step, batch, feature]
        lstm_cell = tf.nn.rnn_cell.LSTMCell(cell_size)
        self.init_state = lstm_cell.zero_state(batch_size=1, dtype=tf.float32)
        # time_major means [time_step, batch, feature] while batch major means [batch, time_step, feature]
        outputs, self.final_state = tf.nn.dynamic_rnn(cell=lstm_cell, inputs=s, initial_state=self.init_state, time_major=True)
        lstm_out = tf.reshape(outputs, [-1, cell_size], name='flatten_rnn_outputs')  # joined state representation
        return lstm_out

    def _build_layers_v2(self, input_dict, num_outputs, options):
        """
        Define the layers of a custom model.
            Arguments:
                input_dict (dict): Dictionary of input tensors, including "obs", "prev_action", "prev_reward", "is_training".
                num_outputs (int): Output tensor must be of size [BATCH_SIZE, num_outputs].
                options (dict): Model options.
            Returns:
                (outputs, feature_layer): Tensors of size [BATCH_SIZE, num_outputs] and [BATCH_SIZE, desired_feature_size].

        When using dict or tuple observation spaces, you can access the nested sub-observation batches here as well:
        Examples:
            >>> print(input_dict)
            {'prev_actions': <tf.Tensor shape=(?,) dtype=int64>,
             'prev_rewards': <tf.Tensor shape=(?,) dtype=float32>,
             'is_training': <tf.Tensor shape=(), dtype=bool>,
             'obs': OrderedDict([
                ('sensors', OrderedDict([
                    ('front_cam', [
                        <tf.Tensor shape=(?, 10, 10, 3) dtype=float32>,
                        <tf.Tensor shape=(?, 10, 10, 3) dtype=float32>]),
                    ('position', <tf.Tensor shape=(?, 3) dtype=float32>),
                    ('velocity', <tf.Tensor shape=(?, 3) dtype=float32>)]))])}
        """
        hidden = 8
        cell_size = 4
        #S = input_dict["obs"]
        S = tf.layers.flatten(input_dict["obs"]) # Flattens an input tensor while preserving the batch axis (axis 0). (deprecated)
        # Example of (optional) weight sharing between two different policies.
        # Here, we share the variables defined in the 'shared' variable scope
        # by entering it explicitly with tf.AUTO_REUSE. This creates the
        # variables for the 'fc1' layer in a global scope called 'shared'
        # outside of the policy's normal variable scope.
        with tf.variable_scope(tf.VariableScope(tf.AUTO_REUSE, "shared"),
                               reuse=tf.AUTO_REUSE,
                               auxiliary_name_scope=False):
            last_layer = tf.layers.dense(S, hidden, activation=tf.nn.relu, name="fc1")
        last_layer = self._lstm(last_layer, cell_size)
        last_layer = tf.layers.dense(last_layer, hidden, activation=tf.nn.relu, name="fc2")
        last_layer = tf.layers.dense(last_layer, hidden, activation=tf.nn.relu, name="fc3")
        #output = tf.layers.dense(last_layer, num_outputs, activation=None, name="fc_out")

        print('********** num_outputs: **********', num_outputs)

        #mu = tf.layers.dense(last_layer, num_outputs, activation=tf.nn.tanh, name="mu") # [-1,1]
        mu = tf.layers.dense(last_layer, num_outputs, activation=tf.nn.softplus, name="mu") # (0, inf)
        sigma = tf.layers.dense(last_layer, num_outputs, activation=tf.nn.softplus, name="sigma") # (0, inf)

        norm_dist = tf.distributions.Normal(loc=mu, scale=sigma)
        output = tf.squeeze(norm_dist.sample(1), axis=0)
        #output = norm_dist.sample(1)
        print('********** output: **********', output)
        return output, last_layer


if __name__ == "__main__":
    args = parser.parse_args()
    ray.init()

    num_of_traders = args.num_agents
    tape_display_length = 100
    tick_size = 1
    init_cash = 10000
    max_step = 10
    MM_env = Exchg(num_of_traders, init_cash, tick_size, tape_display_length, max_step)
    print('MM_env:', MM_env.print_accs())
    register_env("MMenv-v0", lambda _: Exchg(num_of_traders, init_cash, tick_size, tape_display_length, max_step))
    ModelCatalog.register_custom_model("model1", CustomModel1)
    obs_space = MM_env.observation_space
    act_space = MM_env.action_space

    # Each policy can have a different configuration (including custom model)
    def gen_policy(i):
        config = {"model": {"custom_model": "model1"},
                  "gamma": 0.99,}
        return (None, obs_space, act_space, config)

    # Setup PPO with an ensemble of `num_policies` different policies
    policies = {"policy_{}".format(i): gen_policy(i) for i in range(args.num_policies)}
    policy_ids = list(policies.keys())

    tune.run("PPO",
             stop={"training_iteration": args.num_iters},
             config={"env": "MMenv-v0",
                     "log_level": "DEBUG",
                     "simple_optimizer": args.simple,
                     "num_sgd_iter": 10,
                     #'observation_filter': 'MeanStdFilter'
                     "multiagent": {"policies": policies,
                                    "policy_mapping_fn": tune.function(lambda agent_id: random.choice(policy_ids)),
                                   },
                    },
            )
