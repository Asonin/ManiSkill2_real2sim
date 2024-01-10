from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional
from copy import deepcopy

import numpy as np
import sapien.core as sapien
from sapien.core import Pose
from transforms3d.euler import euler2quat
from transforms3d.quaternions import axangle2quat, qmult

from mani_skill2 import ASSET_DIR, format_path
from mani_skill2.utils.common import random_choice
from mani_skill2.utils.io_utils import load_json
from mani_skill2.utils.registration import register_env
from mani_skill2.utils.sapien_utils import vectorize_pose
from mani_skill2.utils.geometry import (
    get_axis_aligned_bbox_for_actor,
    angle_between_vec
)

from .base_env import CustomSceneEnv


class MoveNearInSceneEnv(CustomSceneEnv):
    DEFAULT_ASSET_ROOT: str
    DEFAULT_SCENE_ROOT: str
    DEFAULT_MODEL_JSON: str

    obj: sapien.Actor  # target object

    def __init__(
        self,
        asset_root: str = None,
        scene_root: str = None,
        scene_name: str = None,
        scene_offset: Optional[List[float]] = None,
        scene_pose: Optional[List[float]] = None,
        scene_table_height: float = 0.85,
        model_json: str = None,
        model_ids: List[str] = (),
        **kwargs,
    ):
        if asset_root is None:
            asset_root = self.DEFAULT_ASSET_ROOT
        self.asset_root = Path(format_path(asset_root))
        
        if scene_root is None:
            scene_root = self.DEFAULT_SCENE_ROOT
        self.scene_root = Path(format_path(scene_root))
        self.scene_name = scene_name
        self.scene_offset = scene_offset
        self.scene_pose = scene_pose
        self.scene_table_height = scene_table_height

        if model_json is None:
            model_json = self.DEFAULT_MODEL_JSON
        # NOTE(jigu): absolute path will overwrite asset_root
        model_json = self.asset_root / format_path(model_json)

        if not model_json.exists():
            raise FileNotFoundError(
                f"{model_json} is not found."
                "Please download the corresponding assets:"
                "`python -m mani_skill2.utils.download_asset ${ENV_ID}`."
            )
        self.model_db: Dict[str, Dict] = load_json(model_json)

        if isinstance(model_ids, str):
            raise ValueError(f"For MoveNear, model_ids must be a list of strings; got {model_ids}")
        if len(model_ids) == 0:
            model_ids = sorted(self.model_db.keys())
        assert len(model_ids) >= 3, f"You need to have at least 3 objects for MoveNear; Got {model_ids}"
        self.model_ids = model_ids
        
        self.episode_model_ids = model_ids[:3]
        self.episode_model_scales = [None] * 3
        self.episode_model_bbox_sizes = [None] * 3
        self.episode_model_init_xyz = [None] * 3

        self.arena = None
        
        self.obj = None
        
        self.obj_init_options = {}
        self.robot_init_options = {}
                
        self._check_assets()
        
        super().__init__(**kwargs)

    # def _setup_lighting(self):
    #     super()._setup_lighting()
    #     self._scene.add_directional_light([-1, 1, -1], [1, 1, 1])
        
    def _check_assets(self):
        """Check whether the assets exist."""
        pass

    def _load_actors(self):
        builder = self._scene.create_actor_builder()
        if self.scene_name is None:
            scene_path = str(self.scene_root / "stages/google_pick_coke_can_1_v3.glb") # hardcoded for now
        else:
            scene_path = str(self.scene_root / "stages" / f"{self.scene_name}.glb")
        if self.scene_offset is None:
            scene_offset = np.array([-1.6616, -3.0337, 0.0])
        else:
            scene_offset = np.array(self.scene_offset)
        if self.scene_pose is None:
            scene_pose = sapien.Pose(q=[0.707, 0.707, 0, 0])  # y-axis up for Habitat scenes
        else:
            scene_pose = sapien.Pose(q=self.scene_pose)
        
        # NOTE: use nonconvex collision for static scene
        builder.add_nonconvex_collision_from_file(scene_path, scene_pose)
        builder.add_visual_from_file(scene_path, scene_pose)
        self.arena = builder.build_static()
        # Add offset so that the workspace is next to the table
        
        self.arena.set_pose(sapien.Pose(-scene_offset))
        
        self._load_model()
        self.obj.set_damping(0.1, 0.1)
            
    def _load_model(self):
        """Load the target object."""
        raise NotImplementedError
    
    def reset(self, seed=None, options=None):
        
        if options is None:
            options = dict()
            
        options = deepcopy(options)
        self.obj_init_options = options.pop("obj_init_options", {})
        self.robot_init_options = options.pop("robot_init_options", {})
        
        self.set_episode_rng(seed)
        model_scale = options.pop("model_scale", None)
        model_id = options.pop("model_id", None)
        reconfigure = options.pop("reconfigure", False)
        _reconfigure = self._set_model(model_id, model_scale)
        reconfigure = _reconfigure or reconfigure
                
        options["reconfigure"] = reconfigure
        
        self.episode_model_ids = model_ids[:3]
        self.episode_model_scales = [None] * 3
        self.episode_model_bbox_sizes = [None] * 3
        self.episode_model_init_xyz = [None] * 3
        
        return super().reset(seed=self._episode_seed, options=options)

    # def _setup_lighting(self):
    #     super()._setup_lighting()
    #     # self._scene.add_directional_light([0, 0, -1], [1, 1, 1])
    #     self._scene.add_point_light([-0.2, 0.0, 1.4], [1, 1, 1])
        
    def _set_model(self, model_id, model_scale):
        """Set the model id and scale. If not provided, choose one randomly."""
        reconfigure = False

        if model_id is None:
            model_id = random_choice(self.model_ids, self._episode_rng)
        if model_id != self.model_id:
            self.model_id = model_id
            reconfigure = True

        if model_scale is None:
            model_scales = self.model_db[self.model_id].get("scales")
            if model_scales is None:
                model_scale = 1.0
            else:
                model_scale = random_choice(model_scales, self._episode_rng)
        if model_scale != self.model_scale:
            self.model_scale = model_scale
            reconfigure = True

        model_info = self.model_db[self.model_id]
        if "bbox" in model_info:
            bbox = model_info["bbox"]
            bbox_size = np.array(bbox["max"]) - np.array(bbox["min"])
            self.model_bbox_size = bbox_size * self.model_scale
        else:
            self.model_bbox_size = None

        return reconfigure
    
    def _settle(self, t):
        sim_steps = int(self.sim_freq * t)
        for _ in range(sim_steps):
            self._scene.step()

    def _initialize_actors(self):
        # The object will fall from a certain initial height
        obj_init_xy = self.obj_init_options.get("init_xy", None)
        if obj_init_xy is None:
            obj_init_xy = np.array([-0.2, 0.2]) + self._episode_rng.uniform(-0.05, 0.05, [2])
        obj_init_z = self.obj_init_options.get("init_z", self.scene_table_height)
        obj_init_z = obj_init_z + 0.5 # let object fall onto the table
        obj_init_rot_quat = self.obj_init_options.get("init_rot_quat", None)
        p = np.hstack([obj_init_xy, obj_init_z])
        q = [1, 0, 0, 0] if obj_init_rot_quat is None else obj_init_rot_quat

        # Rotate along z-axis
        if self.obj_init_options.get("init_rand_rot_z", False):
            ori = self._episode_rng.uniform(0, 2 * np.pi)
            q = qmult(euler2quat(0, 0, ori), q)

        # Rotate along a random axis by a small angle
        if (init_rand_axis_rot_range := self.obj_init_options.get("init_rand_axis_rot_range", 0.0)) > 0:
            axis = self._episode_rng.uniform(-1, 1, 3)
            axis = axis / max(np.linalg.norm(axis), 1e-6)
            ori = self._episode_rng.uniform(0, init_rand_axis_rot_range)
            q = qmult(q, axangle2quat(axis, ori, True))
        self.obj.set_pose(Pose(p, q))

        # Move the robot far away to avoid collision
        # The robot should be initialized later
        self.agent.robot.set_pose(Pose([-10, 0, 0]))

        # Lock rotation around x and y
        self.obj.lock_motion(0, 0, 0, 1, 1, 0)
        self._settle(0.5)

        # Unlock motion
        self.obj.lock_motion(0, 0, 0, 0, 0, 0)
        # NOTE(jigu): Explicit set pose to ensure the actor does not sleep
        self.obj.set_pose(self.obj.pose)
        self.obj.set_velocity(np.zeros(3))
        self.obj.set_angular_velocity(np.zeros(3))
        self._settle(0.5)   
        
        # Some objects need longer time to settle
        lin_vel = np.linalg.norm(self.obj.velocity)
        ang_vel = np.linalg.norm(self.obj.angular_velocity)
        if lin_vel > 1e-3 or ang_vel > 1e-2:
            self._settle(1.5)
        
        self.obj_height_after_settle = self.obj.pose.p[2]
        
    @property
    def obj_pose(self):
        """Get the center of mass (COM) pose."""
        return self.obj.pose.transform(self.obj.cmass_local_pose)
    
    def _get_obs_extra(self) -> OrderedDict:
        obs = OrderedDict(
            tcp_pose=vectorize_pose(self.tcp.pose),
        )
        if self._obs_mode in ["state", "state_dict"]:
            obs.update(
                obj_pose=vectorize_pose(self.obj_pose),
                tcp_to_obj_pos=self.obj_pose.p - self.tcp.pose.p,
            )
        return obs

    def check_robot_static(self, thresh=0.2):
        # Assume that the last two DoF is gripper
        qvel = self.agent.robot.get_qvel()[:-2]
        return np.max(np.abs(qvel)) <= thresh

    def evaluate(self, **kwargs):
        is_grasped = self.agent.check_grasp(self.obj, max_angle=80)
        if is_grasped:
            self.consecutive_grasp += 1
        else:
            self.consecutive_grasp = 0
            self.lifted_obj_during_consecutive_grasp = False
            
        contacts = self._scene.get_contacts()
        flag = True
        robot_link_names = [x.name for x in self.agent.robot.get_links()]
        for contact in contacts:
            actor_0, actor_1 = contact.actor0, contact.actor1
            other_obj_contact_actor_name = None
            if actor_0.name == self.obj.name:
                other_obj_contact_actor_name = actor_1.name
            elif actor_1.name == self.obj.name:
                other_obj_contact_actor_name = actor_0.name
            if other_obj_contact_actor_name is not None:
                # the object is in contact with an actor
                contact_impulse = np.sum([point.impulse for point in contact.points], axis=0)
                if (other_obj_contact_actor_name not in robot_link_names) and (np.linalg.norm(contact_impulse) > 1e-6):
                    # the object has contact with an actor other than the robot link, so the object is not yet lifted up
                    # print(other_obj_contact_actor_name, np.linalg.norm(contact_impulse))
                    flag = False
                    break

        consecutive_grasp = (self.consecutive_grasp >= 5)
        diff_obj_height = self.obj.pose.p[2] - self.obj_height_after_settle
        self.lifted_obj_during_consecutive_grasp = self.lifted_obj_during_consecutive_grasp or flag
        
        if self.require_lifting_obj_for_success:
            success = self.lifted_obj_during_consecutive_grasp
        else:
            success = consecutive_grasp
        return dict(
            is_grasped=is_grasped,
            consecutive_grasp=consecutive_grasp,
            lifted_object=self.lifted_obj_during_consecutive_grasp,
            lifted_object_significantly=self.lifted_obj_during_consecutive_grasp and (diff_obj_height > 0.02),
            success=success,
        )

    def compute_dense_reward(self, info, **kwargs):
        reward = 0.0
        if info["success"]:
            reward = 1.0
        return reward

    def compute_normalized_dense_reward(self, **kwargs):
        return self.compute_dense_reward(**kwargs) / 1.0


# ---------------------------------------------------------------------------- #
# YCB
# ---------------------------------------------------------------------------- #
def build_actor_ycb(
    model_id: str,
    scene: sapien.Scene,
    scale: float = 1.0,
    physical_material: sapien.PhysicalMaterial = None,
    density=1000,
    root_dir=ASSET_DIR / "mani_skill2_ycb",
):
    builder = scene.create_actor_builder()
    model_dir = Path(root_dir) / "models" / model_id

    collision_file = str(model_dir / "collision.obj")
    builder.add_multiple_collisions_from_file(
        filename=collision_file,
        scale=[scale] * 3,
        material=physical_material,
        density=density,
    )

    visual_file = str(model_dir / "textured.obj")
    builder.add_visual_from_file(filename=visual_file, scale=[scale] * 3)

    actor = builder.build()
    return actor


@register_env("GraspSingleYCBInScene-v0", max_episode_steps=200)
class GraspSingleYCBInSceneEnv(GraspSingleInSceneEnv):
    DEFAULT_ASSET_ROOT = "{ASSET_DIR}/mani_skill2_ycb"
    DEFAULT_SCENE_ROOT = "{ASSET_DIR}/hab2_bench_assets"
    DEFAULT_MODEL_JSON = "info_pick_v0.json"
    _build_actor_func_name = 'build_actor_ycb'
    obj_static_friction = 0.5
    obj_dynamic_friction = 0.5

    def _check_assets(self):
        models_dir = self.asset_root / "models"
        for model_id in self.model_ids:
            model_dir = models_dir / model_id
            if not model_dir.exists():
                raise FileNotFoundError(
                    f"{model_dir} is not found."
                    "Please download (ManiSkill2) YCB models:"
                    "`python -m mani_skill2.utils.download_asset ycb`."
                )

            collision_file = model_dir / "collision.obj"
            if not collision_file.exists():
                raise FileNotFoundError(
                    "convex.obj has been renamed to collision.obj. "
                    "Please re-download YCB models."
                )

    def _load_model(self):
        density = self.model_db[self.model_id].get("density", 1000)
        if self._build_actor_func_name == 'build_actor_ycb':
            build_actor_func = build_actor_ycb
        elif self._build_actor_func_name == 'build_actor_custom':
            build_actor_func = build_actor_custom
        else:
            raise NotImplementedError(self._build_actor_func_name)
        self.obj = build_actor_func(
            self.model_id,
            self._scene,
            scale=self.model_scale,
            density=density,
            physical_material=self._scene.create_physical_material(
                static_friction=self.obj_static_friction, dynamic_friction=self.obj_dynamic_friction, restitution=0.0
            ),
            root_dir=self.asset_root,
        )
        self.obj.name = self.model_id
        
    def _get_init_z(self):
        bbox_min = self.model_db[self.model_id]["bbox"]["min"]
        return -bbox_min[2] * self.model_scale + 0.05

    def _initialize_agent(self):
        if self.robot_uid == "google_robot_static":
            qpos = np.array(
                [-0.2639457174606611,
                0.0831913360274175,
                0.5017611504652179,
                1.156859026208673,
                0.028583671314766423,
                1.592598203487462,
                -1.080652960128774,
                0, 0,
                -0.00285961, 0.7851361]
            )
            robot_init_height = 0.06205 + 0.017 # base height + ground offset
            robot_init_rot_quat = [0, 0, 0, 1]
        elif self.robot_uid == 'widowx':
            qpos = np.array([0, 0, 0, -np.pi, np.pi / 2, 0, 0.037, 0.037])
            robot_init_height = 0.0
            robot_init_rot_quat = [1, 0, 0, 0]
        else:
            raise NotImplementedError(self.robot_uid)
        
        if self.robot_init_options.get("qpos", None) is not None:
            qpos = self.robot_init_options["qpos"]
        self.agent.reset(qpos)
        if self.robot_init_options.get("init_height", None) is not None:
            robot_init_height = self.robot_init_options["init_height"]
        if self.robot_init_options.get("init_rot_quat", None) is not None:
            robot_init_rot_quat = self.robot_init_options["init_rot_quat"]
        
        if (robot_init_xy := self.robot_init_options.get("init_xy", None)) is not None:
            robot_init_xyz = [robot_init_xy[0], robot_init_xy[1], robot_init_height]
        else:
            init_x = self._episode_rng.uniform(0.30, 0.40)
            init_y = self._episode_rng.uniform(0.0, 0.2)
            robot_init_xyz = [init_x, init_y, robot_init_height]
        
        self.agent.robot.set_pose(Pose(robot_init_xyz, robot_init_rot_quat))
        

@register_env("GraspSingleYCBCanInScene-v0", max_episode_steps=200)
class GraspSingleYCBCanInSceneEnv(GraspSingleYCBInSceneEnv):
    
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["002_master_chef_can", "005_tomato_soup_can", "007_tuna_fish_can", "010_potted_meat_can"]
        super().__init__(**kwargs)
    

@register_env("GraspSingleYCBTomatoCanInScene-v0", max_episode_steps=200)
class GraspSingleYCBTomatoCanInSceneEnv(GraspSingleYCBInSceneEnv):
    
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["005_tomato_soup_can"]
        super().__init__(**kwargs)
    
    
@register_env("GraspSingleYCBBoxInScene-v0", max_episode_steps=200)
class GraspSingleYCBBoxInSceneEnv(GraspSingleYCBInSceneEnv):
    
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["003_cracker_box", "004_sugar_box", "008_pudding_box", "009_gelatin_box"]
        super().__init__(**kwargs)
    
        
        
# ---------------------------------------------------------------------------- #
# Custom Assets
# ---------------------------------------------------------------------------- #
def build_actor_custom(
    model_id: str,
    scene: sapien.Scene,
    scale: float = 1.0,
    physical_material: sapien.PhysicalMaterial = None,
    density=1000,
    root_dir=ASSET_DIR / "custom",
):
    builder = scene.create_actor_builder()
    model_dir = Path(root_dir) / "models" / model_id

    collision_file = str(model_dir / "collision.obj")
    builder.add_multiple_collisions_from_file(
        filename=collision_file,
        scale=[scale] * 3,
        material=physical_material,
        density=density,
    )

    visual_file = str(model_dir / "textured.dae")
    builder.add_visual_from_file(filename=visual_file, scale=[scale] * 3)

    actor = builder.build()
    return actor


@register_env("GraspSingleCustomInScene-v0", max_episode_steps=200)
class GraspSingleCustomInSceneEnv(GraspSingleYCBInSceneEnv):
    DEFAULT_ASSET_ROOT = "{ASSET_DIR}/custom"
    DEFAULT_SCENE_ROOT = "{ASSET_DIR}/hab2_bench_assets"
    DEFAULT_MODEL_JSON = "info_pick_custom_v0.json"
    _build_actor_func_name = 'build_actor_custom'
    obj_static_friction = 0.5
    obj_dynamic_friction = 0.5

    def _check_assets(self):
        models_dir = self.asset_root / "models"
        for model_id in self.model_ids:
            model_dir = models_dir / model_id
            if not model_dir.exists():
                raise FileNotFoundError(
                    f"{model_dir} is not found."
                )

            collision_file = model_dir / "collision.obj"
            if not collision_file.exists():
                raise FileNotFoundError(
                    "convex.obj has been renamed to collision.obj. "
                )        
        

class GraspSingleCustomOrientationInSceneEnv(GraspSingleCustomInSceneEnv):
    def __init__(self, upright=False, laid_vertically=False, lr_switch=False, **kwargs):
        self.obj_upright = upright
        self.obj_laid_vertically = laid_vertically
        self.obj_lr_switch = lr_switch
        super().__init__(**kwargs)
        
    def reset(self, seed=None, options=None):
        if options is None:
            options = dict()
            
        obj_init_options = options.pop("obj_init_options", None)
        if obj_init_options is None:
            obj_init_options = dict()
            
        if self.obj_upright:
            obj_init_options['init_rot_quat'] = euler2quat(np.pi/2, 0, 0)
        elif self.obj_laid_vertically:
            obj_init_options['init_rot_quat'] = euler2quat(0, 0, np.pi/2)
        elif self.obj_lr_switch:
            obj_init_options['init_rot_quat'] = euler2quat(0, 0, np.pi)
            
        options['obj_init_options'] = obj_init_options
            
        return super().reset(seed=seed, options=options)

    
@register_env("GraspSingleCokeCanInScene-v0", max_episode_steps=200)
class GraspSingleCokeCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["coke_can"]
        super().__init__(**kwargs)

        
@register_env("GraspSingleOpenedCokeCanInScene-v0", max_episode_steps=200)
class GraspSingleOpenedCokeCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_coke_can"]
        super().__init__(**kwargs)
    
    
@register_env("GraspSinglePepsiCanInScene-v0", max_episode_steps=200)
class GraspSinglePepsiCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["pepsi_can"]
        super().__init__(**kwargs)
        
@register_env("GraspSingleOpenedPepsiCanInScene-v0", max_episode_steps=200)
class GraspSingleOpenedPepsiCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_pepsi_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingle7upCanInScene-v0", max_episode_steps=200)
class GraspSingle7upCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["7up_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleOpened7upCanInScene-v0", max_episode_steps=200)
class GraspSingleOpened7upCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_7up_can"]
        super().__init__(**kwargs)
    

@register_env("GraspSingleSpriteCanInScene-v0", max_episode_steps=200)
class GraspSingleSpriteCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["sprite_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleOpenedSpriteCanInScene-v0", max_episode_steps=200)
class GraspSingleOpenedSpriteCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_sprite_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleFantaCanInScene-v0", max_episode_steps=200)
class GraspSingleFantaCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["fanta_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleOpenedFantaCanInScene-v0", max_episode_steps=200)
class GraspSingleOpenedFantaCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_fanta_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleRedBullCanInScene-v0", max_episode_steps=200)
class GraspSingleRedBullCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["redbull_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleOpenedRedBullCanInScene-v0", max_episode_steps=200)
class GraspSingleOpenedRedBullCanInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["opened_redbull_can"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleBluePlasticBottleInScene-v0", max_episode_steps=200)
class GraspSingleBluePlasticBottleInSceneEnv(GraspSingleCustomOrientationInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["blue_plastic_bottle"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleAppleInScene-v0", max_episode_steps=200)
class GraspSingleAppleInSceneEnv(GraspSingleCustomInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["apple"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleOrangeInScene-v0", max_episode_steps=200)
class GraspSingleOrangeInSceneEnv(GraspSingleCustomInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["orange"]
        super().__init__(**kwargs)
        

@register_env("GraspSingleSpongeInScene-v0", max_episode_steps=200)
class GraspSingleSpongeInSceneEnv(GraspSingleCustomInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["sponge"]
        super().__init__(**kwargs)


@register_env("GraspSingleBridgeSpoonInScene-v0", max_episode_steps=200)
class GraspSingleBridgeSpoonInSceneEnv(GraspSingleCustomInSceneEnv):
    def __init__(self, **kwargs):
        kwargs.pop('model_ids', None)
        kwargs['model_ids'] = ["bridge_spoon_generated_modified"]
        super().__init__(**kwargs)
