from collections import OrderedDict
from typing import List, Optional

import numpy as np
import sapien.core as sapien
from transforms3d.euler import euler2quat
from transforms3d.quaternions import quat2mat

from mani_skill2.utils.common import random_choice
from mani_skill2.utils.registration import register_env
from mani_skill2.utils.sapien_utils import vectorize_pose

from .base_env import CustomSceneEnv, CustomOtherObjectsInSceneEnv

class MoveNearInSceneEnv(CustomSceneEnv):
    DEFAULT_ASSET_ROOT: str
    DEFAULT_SCENE_ROOT: str
    DEFAULT_MODEL_JSON: str

    obj: sapien.Actor  # target object
    
    def __init__(
        self,
        original_lighting: bool = False,
        **kwargs,
    ):
        self.episode_objs = [None] * 3
        self.episode_model_ids = [None] * 3
        self.episode_model_scales = [None] * 3
        self.episode_model_bbox_sizes = [None] * 3
        self.episode_model_init_xyzs = [None] * 3
        self.episode_obj_heights_after_settle = [None] * 3
        self.episode_source_obj = None
        self.episode_target_obj = None
        self.episode_source_obj_bbox_world = None
        self.episode_target_obj_bbox_world = None
        self.episode_obj_xyzs_after_settle = [None] * 3
        self.episode_source_obj_xyz_after_settle = None
        self.episode_target_obj_xyz_after_settle = None
        
        self.obj_init_options = {}
        
        self.original_lighting = original_lighting
        
        super().__init__(**kwargs)

    def _setup_lighting(self):
        if self.bg_name is not None:
            return

        shadow = self.enable_shadow
        self._scene.set_ambient_light([0.3, 0.3, 0.3])
        if self.original_lighting:
            self._scene.add_directional_light(
                [1, 1, -1], [1, 1, 1], shadow=shadow, scale=5, shadow_map_size=2048
            )
            self._scene.add_directional_light([0, 0, -1], [1, 1, 1])
            return
        
        self._scene.add_directional_light(
            [0, 0, -1], [2.2, 2.2, 2.2], shadow=shadow, scale=5, shadow_map_size=2048
        )
        self._scene.add_directional_light(
            [-1, -0.5, -1], [0.7, 0.7, 0.7]
        )
        self._scene.add_directional_light(
            [1, 1, -1], [0.7, 0.7, 0.7]
        )
        
    def _load_actors(self):
        self._load_arena_helper()        
        self._load_model()
        for obj in self.episode_objs:
            obj.set_damping(0.1, 0.1)
            
    def _load_model(self):
        """Load the target object."""
        raise NotImplementedError
    
    def reset(self, seed=None, options=None):
        if options is None:
            options = dict()
        
        self.obj_init_options = options.get("obj_init_options", {})
        
        self.set_episode_rng(seed)
        model_scales = options.get("model_scales", None)
        model_ids = options.get("model_ids", None)
        reconfigure = options.get("reconfigure", False)
        _reconfigure = self._set_model(model_ids, model_scales)
        reconfigure = _reconfigure or reconfigure
                
        options["reconfigure"] = reconfigure
        
        return super().reset(seed=self._episode_seed, options=options)

    # def _setup_lighting(self):
    #     super()._setup_lighting()
    #     # self._scene.add_directional_light([0, 0, -1], [1, 1, 1])
    #     self._scene.add_point_light([-0.2, 0.0, 1.4], [1, 1, 1])
        
    @staticmethod
    def _list_equal(l1, l2):
        if len(l1) != len(l2):
            return False
        for i in range(len(l1)):
            if l1[i] != l2[i]:
                return False
        return True
    
    def _set_model(self, model_ids, model_scales):
        """Set the model id and scale. If not provided, choose one randomly."""
        reconfigure = False

        if model_ids is None:
            model_ids = []
            for _ in range(3):
                model_ids.append(random_choice(self.model_ids, self._episode_rng))
        if not self._list_equal(model_ids, self.episode_model_ids):
            self.episode_model_ids = model_ids
            reconfigure = True

        if model_scales is None:
            model_scales = []
            for model_id in self.episode_model_ids:
                this_available_model_scales = self.model_db[model_id].get("scales", None)
                if this_available_model_scales is None:
                    model_scales.append(1.0)
                else:
                    model_scales.append(random_choice(this_available_model_scales, self._episode_rng))
        if not self._list_equal(model_scales, self.episode_model_scales):
            self.episode_model_scales = model_scales
            reconfigure = True
        
        model_bbox_sizes = []
        for model_id, model_scale in zip(self.episode_model_ids, self.episode_model_scales):
            model_info = self.model_db[model_id]
            if "bbox" in model_info:
                bbox = model_info["bbox"]
                bbox_size = np.array(bbox["max"]) - np.array(bbox["min"])
                model_bbox_sizes.append(bbox_size * model_scale)
            else:
                raise ValueError(f"Model {model_id} does not have bbox info.")
        self.episode_model_bbox_sizes = model_bbox_sizes

        return reconfigure
    
    def _settle(self, t):
        sim_steps = int(self.sim_freq * t)
        for _ in range(sim_steps):
            self._scene.step()

    def _initialize_actors(self):
        source_obj_id = self.obj_init_options.get("source_obj_id", None)
        target_obj_id = self.obj_init_options.get("target_obj_id", None)
        assert source_obj_id is not None and target_obj_id is not None
        self.episode_source_obj = self.episode_objs[source_obj_id]
        self.episode_target_obj = self.episode_objs[target_obj_id]
        self.episode_source_obj_bbox_world = self.episode_model_bbox_sizes[source_obj_id]
        self.episode_target_obj_bbox_world = self.episode_model_bbox_sizes[target_obj_id]
        
        # The object will fall from a certain initial height
        obj_init_xys = self.obj_init_options.get("init_xys", None) 
        assert obj_init_xys is not None
        obj_init_xys = np.array(obj_init_xys) # [n_objects, 2]
        assert obj_init_xys.shape == (len(self.episode_objs), 2)
        
        obj_init_z = self.obj_init_options.get("init_z", self.scene_table_height)
        obj_init_z = obj_init_z + 0.5 # let object fall onto the table
        
        obj_init_rot_quats = self.obj_init_options.get("init_rot_quats", None)
        if obj_init_rot_quats is not None:
            obj_init_rot_quats = np.array(obj_init_rot_quats)
            assert obj_init_rot_quats.shape == (len(self.episode_objs), 4)
        else:
            obj_init_rot_quats = np.zeros((len(self.episode_objs), 4))
            obj_init_rot_quats[:, 0] = 1.0
        
        for i, obj in enumerate(self.episode_objs):
            p = np.hstack([obj_init_xys[i], obj_init_z])
            q = obj_init_rot_quats[i]
            obj.set_pose(sapien.Pose(p, q))
            # Lock rotation around x and y
            obj.lock_motion(0, 0, 0, 1, 1, 0)
            
        # Move the robot far away to avoid collision
        # The robot should be initialized later
        self.agent.robot.set_pose(sapien.Pose([-10, 0, 0]))
        
        self._settle(0.5)

        # Unlock motion
        for obj in self.episode_objs:
            obj.lock_motion(0, 0, 0, 0, 0, 0)
            # NOTE(jigu): Explicit set pose to ensure the actor does not sleep
            obj.set_pose(obj.pose)
            obj.set_velocity(np.zeros(3))
            obj.set_angular_velocity(np.zeros(3))
        self._settle(0.5)   
        
        # Some objects need longer time to settle
        lin_vel, ang_vel = 0.0, 0.0
        for obj in self.episode_objs:
            lin_vel += np.linalg.norm(obj.velocity)
            ang_vel += np.linalg.norm(obj.angular_velocity)
        if lin_vel > 1e-3 or ang_vel > 1e-2:
            self._settle(1.5)
        
        self.episode_obj_xyzs_after_settle = []
        for obj in self.episode_objs:
            self.episode_obj_xyzs_after_settle.append(obj.pose.p)
        self.episode_source_obj_xyz_after_settle = self.episode_obj_xyzs_after_settle[source_obj_id]
        self.episode_target_obj_xyz_after_settle = self.episode_obj_xyzs_after_settle[target_obj_id]
        self.episode_source_obj_bbox_world = quat2mat(self.episode_source_obj.pose.q) @ self.episode_source_obj_bbox_world
        self.episode_target_obj_bbox_world = quat2mat(self.episode_target_obj.pose.q) @ self.episode_target_obj_bbox_world
        
    @property
    def source_obj_pose(self):
        """Get the center of mass (COM) pose."""
        return self.episode_source_obj.pose.transform(self.episode_source_obj.cmass_local_pose)
    
    @property
    def target_obj_pose(self):
        """Get the center of mass (COM) pose."""
        return self.episode_target_obj.pose.transform(self.episode_target_obj.cmass_local_pose)
    
    def _get_obs_extra(self) -> OrderedDict:
        obs = OrderedDict(
            tcp_pose=vectorize_pose(self.tcp.pose),
        )
        if self._obs_mode in ["state", "state_dict"]:
            obs.update(
                source_obj_pose=vectorize_pose(self.source_obj_pose),
                target_obj_pose=vectorize_pose(self.target_obj_pose),
                tcp_to_obj_pos=self.source_obj_pose.p - self.tcp.pose.p,
            )
        return obs

    def check_robot_static(self, thresh=0.2):
        # Assume that the last two DoF is gripper
        qvel = self.agent.robot.get_qvel()[:-2]
        return np.max(np.abs(qvel)) <= thresh

    def evaluate(self, **kwargs):
        source_obj_pose = self.source_obj_pose
        target_obj_pose = self.target_obj_pose
        
        all_obj_heights = [obj.pose.p[2] for obj in self.episode_objs]
        diff_obj_heights = [all_obj_heights[i] - self.episode_obj_xyzs_after_settle[i][2] for i in range(len(all_obj_heights))]
        all_obj_keep_height = all([x > -0.02 for x in diff_obj_heights])
        
        source_obj_xy_move_dist = np.linalg.norm(self.episode_source_obj_xyz_after_settle[:2] - self.episode_source_obj.pose.p[:2])
        other_obj_xy_move_dist = []
        for obj, obj_xyz_after_settle in zip(self.episode_objs, self.episode_obj_xyzs_after_settle):
            if obj.name == self.episode_source_obj.name:
                continue
            other_obj_xy_move_dist.append(np.linalg.norm(obj_xyz_after_settle[:2] - obj.pose.p[:2]))
        moved_correct_obj = (all(x < source_obj_xy_move_dist / 2 for x in other_obj_xy_move_dist))
        
        dist_to_tgt_obj = np.linalg.norm(source_obj_pose.p[:2] - target_obj_pose.p[:2])
        tgt_obj_bbox_xy_dist = np.linalg.norm(self.episode_target_obj_bbox_world[:2]) / 2 # get half-length of bbox xy diagonol distance in the world frame at timestep=0
        src_obj_bbox_xy_dist = np.linalg.norm(self.episode_source_obj_bbox_world[:2]) / 2
        near_tgt_obj = (dist_to_tgt_obj < tgt_obj_bbox_xy_dist + src_obj_bbox_xy_dist + 0.04)
        
        dist_to_other_objs = []
        for obj in self.episode_objs:
            if obj.name == self.episode_source_obj.name:
                continue
            dist_to_other_objs.append(np.linalg.norm(source_obj_pose.p[:2] - obj.pose.p[:2]))
        is_closest_to_tgt = all([dist_to_tgt_obj < x + 0.03 for x in dist_to_other_objs])
        
        success = all_obj_keep_height and moved_correct_obj and near_tgt_obj and is_closest_to_tgt
        return dict(
            all_obj_keep_height=all_obj_keep_height,
            moved_correct_obj=moved_correct_obj,
            near_tgt_obj=near_tgt_obj,
            is_closest_to_tgt=is_closest_to_tgt,
            success=success,
        )

    def compute_dense_reward(self, info, **kwargs):
        reward = 0.0
        if info["success"]:
            reward = 1.0
        return reward

    def compute_normalized_dense_reward(self, **kwargs):
        return self.compute_dense_reward(**kwargs) / 1.0
    
    def get_language_instruction(self):
        src_name = self.episode_source_obj.name.replace('_', ' ')
        tgt_name = self.episode_target_obj.name.replace('_', ' ')
        return f"move {src_name} near {tgt_name}"
    
    
    
@register_env("MoveNearGoogleInScene-v0", max_episode_steps=200)
class MoveNearGoogleInSceneEnv(MoveNearInSceneEnv, CustomOtherObjectsInSceneEnv):
    def __init__(
        self,
        **kwargs,
    ):
        self.triplets = [
            ("blue_plastic_bottle", "pepsi_can", "orange"),
            ("7up_can", "apple", "sponge"),
            ("coke_can", "redbull_can", "apple"),
            ("sponge", "blue_plastic_bottle", "7up_can"),
            ("orange", "pepsi_can", "redbull_can"),
        ]
        self._source_obj_ids, self._target_obj_ids = [], []
        for i in range(3):
            for j in range(3):
                if i != j:
                    self._source_obj_ids.append(i)
                    self._target_obj_ids.append(j)
        self._xy_config_per_triplet = [
            ([-0.33, 0.04], [-0.33, 0.34], [-0.13, 0.19]),
            ([-0.13, 0.04], [-0.33, 0.19], [-0.13, 0.34]),
        ]
        self.obj_init_quat_dict = {
            "blue_plastic_bottle": euler2quat(np.pi/2, 0, np.pi/2),
            "pepsi_can": euler2quat(np.pi/2, 0, 0),
            "orange": [1.0, 0.0, 0.0, 0.0],
            "7up_can": euler2quat(np.pi/2, 0, 0),
            "apple": [1.0, 0.0, 0.0, 0.0],
            "sponge": euler2quat(0, 0, np.pi/2),
            "coke_can": euler2quat(np.pi/2, 0, 0),
            "redbull_can": euler2quat(np.pi/2, 0, 0),
        }
        super().__init__(**kwargs)
    
    def reset(self, seed=None, options=None):
        if options is None:
            options = dict()
        
        obj_init_options = options.pop("obj_init_options", {})
        episode_id = obj_init_options.get("episode_id", 0)
        triplet = self.triplets[episode_id // (len(self._source_obj_ids) * len(self._xy_config_per_triplet))]
        source_obj_id = self._source_obj_ids[episode_id % len(self._source_obj_ids)]
        target_obj_id = self._target_obj_ids[episode_id % len(self._target_obj_ids)]
        xy_config_triplet = self._xy_config_per_triplet[
            (episode_id % (len(self._source_obj_ids) * len(self._xy_config_per_triplet))) // len(self._source_obj_ids)
        ]
        quat_config_triplet = [self.obj_init_quat_dict[model_id] for model_id in triplet]
        
        options['model_ids'] = triplet
        obj_init_options['source_obj_id'] = source_obj_id
        obj_init_options['target_obj_id'] = target_obj_id
        obj_init_options['init_xys'] = xy_config_triplet
        obj_init_options['init_rot_quats'] = quat_config_triplet
        options['obj_init_options'] = obj_init_options
        
        return super().reset(seed=seed, options=options)
    
    def _load_model(self):
        self.episode_objs = []
        for (model_id, model_scale) in zip(self.episode_model_ids, self.episode_model_scales):
            density = self.model_db[model_id].get("density", 1000)
            obj = self._build_actor_helper(
                model_id,
                self._scene,
                scale=model_scale,
                density=density,
                physical_material=self._scene.create_physical_material(
                    static_friction=self.obj_static_friction, dynamic_friction=self.obj_dynamic_friction, restitution=0.0
                ),
                root_dir=self.asset_root,
            )
            obj.name = model_id
            self.episode_objs.append(obj)