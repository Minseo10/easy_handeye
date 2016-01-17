#!/usr/bin/env python2
from __future__ import division
from handeyecalibration.handeye_client import HandeyeClient
from tf.transformations import quaternion_multiply, quaternion_from_euler
from geometry_msgs.msg import Quaternion
from moveit_commander import MoveGroupCommander
from qt_gui.plugin import Plugin
from python_qt_binding import QtGui
import rospy
import numpy as np
from itertools import chain, izip
from copy import deepcopy
import math
import sys


class CalibrationMovements:
    def __init__(self, move_group_name):
        #self.client = HandeyeClient()  # TODO: move around marker when eye_on_hand
        self.mgc = MoveGroupCommander(move_group_name)
        self.mgc.set_planner_id("RRTConnectkConfigDefault")
        self.start_pose = self.mgc.get_current_pose()
        self.poses = []
        self.fallback_joint_limits = [math.radians(90)]*4+[math.radians(90)]+[math.radians(180)]+[math.radians(350)]
        if len(self.mgc.get_active_joints()) == 6:
            self.fallback_joint_limits = self.fallback_joint_limits[1:]

    def compute_poses_around_current_state(self, angle_delta, translation_delta):
        self.start_pose = self.mgc.get_current_pose()
        basis = np.eye(3)

        pos_deltas = [quaternion_from_euler(*rot_axis*angle_delta) for rot_axis in basis]
        neg_deltas = [quaternion_from_euler(*rot_axis*(-angle_delta)) for rot_axis in basis]

        quaternion_deltas = list(chain.from_iterable(izip(pos_deltas, neg_deltas)))  # interleave

        final_rots = []
        for qd in quaternion_deltas:
            final_rots.append(list(qd))

        final_poses = []
        for rot in final_rots:
            fp = deepcopy(self.start_pose)
            ori = fp.pose.orientation
            combined_rot = quaternion_multiply([ori.x, ori.y, ori.z, ori.w], rot)
            fp.pose.orientation = Quaternion(*combined_rot)
            final_poses.append(fp)

        fp = deepcopy(self.start_pose)
        fp.pose.position.x -= translation_delta
        final_poses.append(fp)

        fp = deepcopy(self.start_pose)
        fp.pose.position.y -= translation_delta
        final_poses.append(fp)

        fp = deepcopy(self.start_pose)
        fp.pose.position.z -= translation_delta
        final_poses.append(fp)

        self.poses = final_poses
        self.current_pose_index = -1

    def check_poses(self, joint_limits):
        if len(self.fallback_joint_limits) == 6:
            joint_limits = joint_limits[1:]
        for fp in self.poses:
            self.mgc.set_pose_target(fp)
            plan = self.mgc.plan()
            if len(plan.joint_trajectory.points) == 0 or CalibrationMovements.is_crazy_plan(plan, joint_limits):
                return False
        return True

    def plan_to_start_pose(self):
        return self.plan_to_pose(self.start_pose)

    def plan_to_pose(self, pose):
        self.mgc.set_start_state_to_current_state()
        self.mgc.set_pose_target(pose)
        plan = self.mgc.plan()
        return plan

    def execute_plan(self, plan):
        if CalibrationMovements.is_crazy_plan(plan, self.fallback_joint_limits):
            raise RuntimeError("got crazy plan!")
        self.mgc.execute(plan)

    @staticmethod
    def rot_per_joint(plan, degrees=False):
        np_traj = np.array([p.positions for p in plan.joint_trajectory.points])
        if len(np_traj) == 0:
            raise ValueError
        np_traj_max_per_joint = np_traj.max(axis=0)
        np_traj_min_per_joint = np_traj.min(axis=0)
        ret = abs(np_traj_max_per_joint - np_traj_min_per_joint)
        if degrees:
            ret = [math.degrees(j) for j in ret]
        return ret

    @staticmethod
    def is_crazy_plan(plan, max_rotation_per_joint):
        abs_rot_per_joint = CalibrationMovements.rot_per_joint(plan)
        if (abs_rot_per_joint > max_rotation_per_joint).any():
            return True
        else:
            return False


class CalibrationMovementsGUI(QtGui.QWidget):
    NOT_INITED_YET = 0
    BAD_PLAN = 1
    GOOD_PLAN = 2
    MOVED_TO_POSE = 3
    BAD_STARTING_POSITION = 4
    GOOD_STARTING_POSITION = 5

    def __init__(self):
        super(CalibrationMovementsGUI, self).__init__()
        move_group_name = rospy.get_param('~move_group', 'manipulator')
        self.angle_delta = rospy.get_param('~angle_delta', math.radians(25))
        self.translation_delta = rospy.get_param('~translation_delta', 0.1)
        self.local_mover = CalibrationMovements(move_group_name)
        self.current_pose = -1
        self.current_plan = None
        self.initUI()
        self.state = CalibrationMovementsGUI.NOT_INITED_YET

    def initUI(self):
        self.layout = QtGui.QVBoxLayout()
        self.labels_layout = QtGui.QHBoxLayout()
        self.buttons_layout = QtGui.QHBoxLayout()

        self.progress_bar = QtGui.QProgressBar()
        self.pose_number_lbl = QtGui.QLabel('0/8')
        self.bad_plan_lbl = QtGui.QLabel('No plan yet')
        self.guide_lbl = QtGui.QLabel('Hello')

        self.check_start_pose_btn = QtGui.QPushButton('Check starting pose')
        self.check_start_pose_btn.clicked.connect(self.handle_check_current_state)

        self.next_pose_btn = QtGui.QPushButton('Next Pose')
        self.next_pose_btn.clicked.connect(self.handle_next_pose)

        self.plan_btn = QtGui.QPushButton('Plan')
        self.plan_btn.clicked.connect(self.handle_plan)

        self.execute_btn = QtGui.QPushButton('Execute')
        self.execute_btn.clicked.connect(self.handle_execute)

        self.labels_layout.addWidget(self.pose_number_lbl)
        self.labels_layout.addWidget(self.bad_plan_lbl)

        self.buttons_layout.addWidget(self.check_start_pose_btn)
        self.buttons_layout.addWidget(self.next_pose_btn)
        self.buttons_layout.addWidget(self.plan_btn)
        self.buttons_layout.addWidget(self.execute_btn)

        self.layout.addWidget(self.progress_bar)
        self.layout.addLayout(self.labels_layout)
        self.layout.addWidget(self.guide_lbl)
        self.layout.addLayout(self.buttons_layout)

        self.setLayout(self.layout)

        self.plan_btn.setEnabled(False)
        self.execute_btn.setEnabled(False)

        self.setWindowTitle('Local Mover')
        self.show()

    def updateUI(self):
        self.progress_bar.setMaximum(len(self.local_mover.poses))
        self.progress_bar.setValue(self.current_pose + 1)
        self.pose_number_lbl.setText('{}/{}'.format(self.current_pose+1, len(self.local_mover.poses)))

        if self.state == CalibrationMovementsGUI.BAD_PLAN:
            self.bad_plan_lbl.setText('BAD plan!! Don\'t do it!!!!')
            self.bad_plan_lbl.setStyleSheet('QLabel { background-color : red}')
        elif self.state == CalibrationMovementsGUI.GOOD_PLAN:
            self.bad_plan_lbl.setText('Good plan')
            self.bad_plan_lbl.setStyleSheet('QLabel { background-color : green}')
        else:
            self.bad_plan_lbl.setText('No plan yet')
            self.bad_plan_lbl.setStyleSheet('')

        if self.state == CalibrationMovementsGUI.NOT_INITED_YET:
            self.guide_lbl.setText('Bring the robot to a plausible position and check if it is a suitable starting pose')
        elif self.state == CalibrationMovementsGUI.BAD_STARTING_POSITION:
            self.guide_lbl.setText('Cannot calibrate from current position')
        elif self.state == CalibrationMovementsGUI.GOOD_STARTING_POSITION:
            self.guide_lbl.setText('Ready to start: click on next pose')
        elif self.state == CalibrationMovementsGUI.GOOD_PLAN:
            self.guide_lbl.setText('The plan seems good: press execute to move the robot')
        elif self.state == CalibrationMovementsGUI.BAD_PLAN:
            self.guide_lbl.setText('Planning failed: try again')
        elif self.state == CalibrationMovementsGUI.MOVED_TO_POSE:
            self.guide_lbl.setText('Pose reached: take a sample and go on to next pose')

        can_plan = self.state == CalibrationMovementsGUI.GOOD_STARTING_POSITION
        self.plan_btn.setEnabled(can_plan)
        can_move = self.state == CalibrationMovementsGUI.GOOD_PLAN
        self.execute_btn.setEnabled(can_move)


    def handle_check_current_state(self):
        self.local_mover.compute_poses_around_current_state(self.angle_delta, self.translation_delta)

        joint_limits = [math.radians(90)]*5+[math.radians(180)]+[math.radians(350)]  # TODO: make param
        if self.local_mover.check_poses(joint_limits):
            self.state = CalibrationMovementsGUI.GOOD_STARTING_POSITION
        else:
            self.state = CalibrationMovementsGUI.BAD_STARTING_POSITION
        self.current_pose = -1

        self.updateUI()

    def handle_next_pose(self):
        self.guide_lbl.setText('Going to center position')
        if self.current_pose != -1:
            plan = self.local_mover.plan_to_start_pose()
            self.local_mover.execute_plan(plan)
        if self.current_pose < len(self.local_mover.poses)-1:
            self.current_pose += 1
        self.state = CalibrationMovementsGUI.GOOD_STARTING_POSITION
        self.updateUI()

    def handle_plan(self):
        self.guide_lbl.setText('Planning to the next position. Click on execute when a good one was found')
        if self.current_pose >= 0:
            self.current_plan = self.local_mover.plan_to_pose(self.local_mover.poses[self.current_pose])
            if CalibrationMovements.is_crazy_plan(self.current_plan, self.local_mover.fallback_joint_limits):  #TODO: sort out this limits story
                self.state = CalibrationMovementsGUI.BAD_PLAN
            else:
                self.state = CalibrationMovementsGUI.GOOD_PLAN
        self.updateUI()

    def handle_execute(self):
        if self.current_plan != None:
            self.guide_lbl.setText('Going to the selected pose')
            self.local_mover.execute_plan(self.current_plan)
            self.state = CalibrationMovementsGUI.MOVED_TO_POSE
            self.updateUI()


class RqtCalibrationMovements(Plugin):
    def __init__(self, context):
        super(RqtCalibrationMovements, self).__init__(context)
        # Give QObjects reasonable names
        self.setObjectName('LocalMover')

        rospy.sleep(1.0)

        # Process standalone plugin command-line arguments
        from argparse import ArgumentParser
        parser = ArgumentParser()
        # Add argument(s) to the parser.
        parser.add_argument("-q", "--quiet", action="store_true",
                            dest="quiet",
                            help="Put plugin in silent mode")
        args, unknowns = parser.parse_known_args(context.argv())
        if not args.quiet:
            print 'arguments: ', args
            print 'unknowns: ', unknowns

        # Create QWidget
        self._widget = CalibrationMovementsGUI()
        if context.serial_number() > 1:
            self._widget.setWindowTitle(self._widget.windowTitle() + (' (%d)' % context.serial_number()))
        # Add widget to the user interface
        context.add_widget(self._widget)

    def shutdown_plugin(self):
        # TODO unregister all publishers here
        pass

    def save_settings(self, plugin_settings, instance_settings):
        # TODO save intrinsic configuration, usually using:
        # instance_settings.set_value(k, v)
        pass

    def restore_settings(self, plugin_settings, instance_settings):
        # TODO restore intrinsic configuration, usually using:
        # v = instance_settings.value(k)
        pass

    # def trigger_configuration(self):
    # Comment in to signal that the plugin has a way to configure
    # This will enable a setting button (gear icon) in each dock widget title bar
    # Usually used to open a modal configuration dialog

if __name__ == '__main__':

    NODE_NAME = 'handeyecalibration_mover'

    rospy.init_node(NODE_NAME)
    while rospy.get_time() == 0.0:
        pass

    lm = CalibrationMovements()
    rospy.sleep(1.0)

    qapp = QtGui.QApplication(sys.argv)
    lmg = CalibrationMovementsGUI(lm)
    lmg.show()
    sys.exit(qapp.exec_())

    # TODO: alternative workflow for automatic calibration:
    # generate poses around current pose, each rotating by angle_delta in each direction
    # generate a plan to each pose and back
    # if all movements are possible, perform them in a row (at low speed)


