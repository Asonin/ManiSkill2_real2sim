from copy import deepcopy
import numpy as np

from mani_skill2.agents.controllers import *
from mani_skill2.sensors.camera import CameraConfig


class GoogleRobotDefaultConfig:
    def __init__(self, mobile_base=False) -> None:
        if mobile_base:
            self.urdf_path = "{PACKAGE_ASSET_DIR}/descriptions/googlerobot_description/google_robot_meta_sim_fix_fingertip.urdf"
        else:
            self.urdf_path = "{PACKAGE_ASSET_DIR}/descriptions/googlerobot_description/google_robot_meta_sim_fix_wheel_fix_fingertip.urdf"
        # standard urdf does not support <contact> tag, so we manually define friction here
        self.urdf_config = dict(
            _materials=dict(
                finger_mat=dict(static_friction=2.0, dynamic_friction=2.0, restitution=0.0),
                finger_tip_mat=dict(static_friction=2.0, dynamic_friction=2.0, restitution=0.0),
                finger_nail_mat=dict(static_friction=0.1, dynamic_friction=0.1, restitution=0.0),
                base_mat=dict(static_friction=0.1, dynamic_friction=0.0, restitution=0.0),
                wheel_mat=dict(static_friction=1.0, dynamic_friction=0.0, restitution=0.0),
            ),
            link=dict(
                link_base=dict(
                    material="base_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_wheel_left=dict(
                    material="wheel_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_wheel_right=dict(
                    material="wheel_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_left=dict(
                    material="finger_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_right=dict(
                    material="finger_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_tip_left=dict(
                    material="finger_tip_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_tip_right=dict(
                    material="finger_tip_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_nail_left=dict(
                    material="finger_nail_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
                link_finger_nail_right=dict(
                    material="finger_nail_mat", patch_radius=0.1, min_patch_radius=0.1
                ),
            ),
        )
        
        self.base_joint_names = ['joint_wheel_left', 'joint_wheel_right']
        self.base_damping = 1e3
        self.base_force_limit = 500
        self.mobile_base = mobile_base # whether the robot base is mobile
        
        self.arm_joint_names = ['joint_torso', 'joint_shoulder', 'joint_bicep', 'joint_elbow', 
                                'joint_forearm', 'joint_wrist', 'joint_gripper', 'joint_head_pan', 'joint_head_tilt']
        self.arm_stiffness = 1e3 # TODO: arm and gripper both need system identification
        self.arm_damping = 1e2
        self.arm_force_limit = 100

        self.gripper_finger_joint_names = ['joint_finger_right', 'joint_finger_left']
        self.gripper_finger_stiffness = 1e3 # TODO: arm and gripper both need system identification
        self.gripper_finger_damping = 1e2
        self.gripper_finger_force_limit = 100
        
        self.gripper_finger_tip_joint_names = ['joint_finger_tip_right', 'joint_finger_tip_left']
        self.gripper_finger_tip_stiffness = 1e3 # TODO: arm and gripper both need system identification
        self.gripper_finger_tip_damping = 1e2
        self.gripper_finger_tip_force_limit = 100

        self.ee_link_name = "link_gripper_tcp"

    @property
    def controllers(self):
        _C = {}
        
        # -------------------------------------------------------------------------- #
        # Base
        # -------------------------------------------------------------------------- #
        if self.mobile_base:
            _C["base"] = dict(
                # PD ego-centric joint velocity
                base_pd_joint_vel=PDBaseVelControllerConfig(
                    self.base_joint_names,
                    lower=[-0.5, -0.5],
                    upper=[0.5, 0.5],
                    damping=self.base_damping,
                    force_limit=self.base_force_limit,
                )
            )
        else:
            _C["base"] = [None]
        
        # -------------------------------------------------------------------------- #
        # Arm
        # -------------------------------------------------------------------------- #
        arm_pd_ee_delta_pose = PDEEPoseControllerConfig(
            self.arm_joint_names,
            -0.1,
            0.1,
            0.1,
            self.arm_stiffness,
            self.arm_damping,
            self.arm_force_limit,
            ee_link=self.ee_link_name,
            frame="ee",
        )
        arm_pd_ee_delta_pose_base = PDEEPoseControllerConfig(
            self.arm_joint_names,
            -0.1,
            0.1,
            0.1,
            self.arm_stiffness,
            self.arm_damping,
            self.arm_force_limit,
            ee_link=self.ee_link_name,
            frame="base",
        )
        arm_pd_ee_target_delta_pose_base = PDEEPoseControllerConfig(
            self.arm_joint_names,
            -0.1,
            0.1,
            0.1,
            self.arm_stiffness,
            self.arm_damping,
            self.arm_force_limit,
            ee_link=self.ee_link_name,
            frame="base",
            use_target=True,
        )
        _C["arm"] = dict(
            arm_pd_ee_delta_pose=arm_pd_ee_delta_pose,
            arm_pd_ee_delta_pose_base=arm_pd_ee_delta_pose_base,
            arm_pd_ee_target_delta_pose_base=arm_pd_ee_target_delta_pose_base,
        )

        # -------------------------------------------------------------------------- #
        # Gripper
        # -------------------------------------------------------------------------- #
        gripper_finger_pd_joint_pos = PDJointPosMimicControllerConfig(
            self.gripper_finger_joint_names,
            -1e-4,
            1.3,
            self.gripper_finger_stiffness,
            self.gripper_finger_damping,
            self.gripper_finger_force_limit,
        )
        gripper_finger_pd_joint_delta_pos = PDJointPosMimicControllerConfig(
            self.gripper_finger_joint_names,
            -1.0,
            1.0,
            self.gripper_finger_stiffness,
            self.gripper_finger_damping,
            self.gripper_finger_force_limit,
            use_delta=True,
        )
        _C["gripper_finger"] = dict(
            gripper_finger_pd_joint_pos=gripper_finger_pd_joint_pos,
            gripper_finger_pd_joint_delta_pos=gripper_finger_pd_joint_delta_pos,
        )

        controller_configs = {}
        for base_controller_name in _C["base"]:
            for arm_controller_name in _C["arm"]:
                for gripper_controller_name in _C["gripper_finger"]:
                    c = {}
                    if base_controller_name is not None:
                        c = {"base": _C["base"][base_controller_name]}
                    c["arm"] = _C["arm"][arm_controller_name]
                    c["gripper_finger"] = _C["gripper_finger"][gripper_controller_name]
                    combined_name = arm_controller_name + "_" + gripper_controller_name
                    if base_controller_name is not None:
                        combined_name = base_controller_name + "_" + combined_name
                    controller_configs[combined_name] = c

        # Make a deepcopy in case users modify any config
        return deepcopy_dict(controller_configs)

    @property
    def cameras(self):
        return CameraConfig(
            uid="overhead_camera",
            p=[0, 0, 0],
            q=[0.5, 0.5, -0.5, 0.5], # SAPIEN uses ros camera convention; the rotation matrix of link_camera is in opencv convention, so we need to transform it to ros convention
            width=640,
            height=512,
            fov=1.5,
            near=0.01,
            far=10,
            actor_uid="link_camera",
            intrinsic=np.array([[425.0, 0, 320.0], [0, 425.0, 256.0], [0, 0, 1]]),
        )
        
        
class GoogleRobotStaticBaseConfig(GoogleRobotDefaultConfig):
    
    def __init__(self) -> None:
        super().__init__(mobile_base=False)
        
        
class GoogleRobotMobileBaseConfig(GoogleRobotDefaultConfig):
    
    def __init__(self) -> None:
        super().__init__(mobile_base=True)