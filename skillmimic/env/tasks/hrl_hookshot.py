# Copyright (c) 2018-2022, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import torch
from torch import Tensor
from typing import Tuple
from enum import Enum

from utils import torch_utils
from utils.motion_data_handler import MotionDataHandler

from isaacgym import gymapi
from isaacgym import gymtorch
from isaacgym.torch_utils import *

from env.tasks.humanoid_object_task import HumanoidWholeBodyWithObject

TAR_ACTOR_ID = 1
TAR_FACING_ACTOR_ID = 2

class HRLHookshot(HumanoidWholeBodyWithObject):
    class StateInit(Enum):
        Default = 0
        Start = 1
        Random = 2
        Hybrid = 3

    def __init__(self, cfg, sim_params, physics_engine, device_type, device_id, headless):
        state_init = cfg["env"]["stateInit"]
        self._state_init = HRLHookshot.StateInit[state_init]
        
        # self._enable_task_obs = False #cfg["env"]["enableTaskObs"]
        
        # self.condition_size = 0
        self.goal_size = 0

        self.motion_file = cfg['env']['motion_file']
        self.play_dataset = cfg['env']['playdataset']
        self.robot_type = cfg["env"]["asset"]["assetFileName"]
        self.reward_weights_default = cfg["env"]["rewardWeights"]
        self.save_images = cfg['env']['saveImages']
        self.init_vel = cfg['env']['initVel']
        self.ball_size = cfg['env']['ballSize']

        super().__init__(cfg=cfg,
                         sim_params=sim_params,
                         physics_engine=physics_engine,
                         device_type=device_type,
                         device_id=device_id,
                         headless=headless)
        
        self._load_motion(self.motion_file)

        self._goal_position = torch.zeros([self.num_envs, 2], device=self.device, dtype=torch.float)

        self.reached_target = torch.zeros(
            self.num_envs, device=self.device, dtype=torch.bool)

        self._termination_heights = torch.tensor(self.cfg["env"]["terminationHeight"], device=self.device, dtype=torch.float)

        
        return

    def get_task_obs_size(self):
        if (self._enable_task_obs):
            obs_size = self.goal_size
        else:
            obs_size = 0
        return obs_size

    def _load_motion(self, motion_file):
        self.skill_name = motion_file.split('/')[-1] #metric
        # self.max_episode_length = 800

        self._motion_data = MotionDataHandler(motion_file, self.device, self._key_body_ids, self.cfg, self.num_envs, self.max_episode_length, self.init_vel)

        return

    def _reset_actors(self, env_ids):
        if self._state_init == HRLHookshot.StateInit.Start \
              or self._state_init == HRLHookshot.StateInit.Random:
            self._reset_random_ref_state_init(env_ids) #V1 Random Ref State Init (RRSI)
        else:
            assert(False), "Unsupported state initialization strategy: {:s}".format(str(self._state_init))

        super()._reset_actors(env_ids)

        return

    def _reset_humanoid(self, env_ids):
        self._humanoid_root_states[env_ids, 0:3] = self.init_root_pos[env_ids]
        self._humanoid_root_states[env_ids, 3:7] = self.init_root_rot[env_ids]
        self._humanoid_root_states[env_ids, 7:10] = self.init_root_pos_vel[env_ids]
        self._humanoid_root_states[env_ids, 10:13] = self.init_root_rot_vel[env_ids]
        
        self._dof_pos[env_ids] = self.init_dof_pos[env_ids]
        self._dof_vel[env_ids] = self.init_dof_pos_vel[env_ids]
        return


    def _reset_random_ref_state_init(self, env_ids): #Z11
        num_envs = env_ids.shape[0]

        motion_ids = self._motion_data.sample_motions(num_envs)
        motion_times = self._motion_data.sample_time(motion_ids)

        self.reward_weights, self.hoi_data_batch, \
        self.init_root_pos[env_ids], self.init_root_rot[env_ids],  self.init_root_pos_vel[env_ids], self.init_root_rot_vel[env_ids], \
        self.init_dof_pos[env_ids], self.init_dof_pos_vel[env_ids], \
        self.init_obj_pos[env_ids], self.init_obj_pos_vel[env_ids], self.init_obj_rot[env_ids], self.init_obj_rot_vel[env_ids] \
            = self._motion_data.get_initial_state(env_ids, motion_ids, motion_times, self.reward_weights_default)

        # if self.show_motion_test == False:
        #     print('motionid:', self.hoi_data_dict[int(self.envid2motid[0])]['hoi_data_text'], \
        #         'motionlength:', self.hoi_data_dict[int(self.envid2motid[0])]['hoi_data'].shape[0]) #ZC
        #     self.show_motion_test = True

        return

    def _compute_reset(self):
        root_pos = self._humanoid_root_states[..., 0:3]
        self.reset_buf[:], self._terminate_buf[:] = compute_humanoid_reset(self.reset_buf, self.progress_buf,
                                                                           self._contact_forces,self._rigid_body_pos,self._target_states[..., 0:3],
                                                                            root_pos, self._goal_position,
                                                                            self.max_episode_length, self._enable_early_termination, self._termination_heights
                                                                            )
        return
    
    def _compute_reward(self, actions):
        ball_pos = self._target_states[..., 0:3]
        self.rew_buf[:] = compute_hook_reward(ball_pos)
        return

    def _reset_envs(self, env_ids):

        super()._reset_envs(env_ids)  

        return

    def _compute_observations(self, env_ids=None):
        obs = self._compute_humanoid_obs(env_ids)

        obj_obs = self._compute_obj_obs(env_ids)

        obs = torch.cat([obs, obj_obs], dim=-1)

        if (env_ids is None):
            self.obs_buf[:] = obs
        else:
            self.obs_buf[env_ids] = obs

        return

    def get_num_amp_obs(self):
        return 323 + len(self.cfg["env"]["keyBodies"])*3 + 6  #0
    
#####################################################################
###=========================jit functions=========================###
#####################################################################



# @torch.jit.script
def compute_hook_reward(ball_pos):

    ball_height_reward = torch.exp(-torch.abs(ball_pos[:, 2]-2.5))
    reward = ball_height_reward

    return reward

# @torch.jit.script
def compute_humanoid_reset(reset_buf, progress_buf, contact_buf, rigid_body_pos, ball_pos, root_pos, goal_pos,
                           max_episode_length, enable_early_termination, termination_heights):
    # type: (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, float, bool, Tensor) -> Tuple[Tensor, Tensor]
    
    terminated = torch.zeros_like(reset_buf)

    if (enable_early_termination):
        has_fallen = root_pos[..., 2] < termination_heights
        has_fallen *= (progress_buf > 1) # 本质就是 与
        terminated = torch.where(has_fallen, torch.ones_like(reset_buf), terminated)
    
    reset = torch.where(progress_buf >= max_episode_length -1, torch.ones_like(reset_buf), terminated)

    return reset, terminated