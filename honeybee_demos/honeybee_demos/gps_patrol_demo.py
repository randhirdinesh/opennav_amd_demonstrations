#! /usr/bin/env python3
# Copyright 2024 Open Naviation LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
from threading import Lock, Thread
import time

from geographic_msgs.msg import GeoPose
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy
from rclpy.qos import QoSProfile, QoSReliabilityPolicy
from robot_localization.srv import SetDatum
from sensor_msgs.msg import BatteryState, Joy

###################################################################################################
# For a deployed production application, these would be stored on the robot to run on some
# frequency, cloud operations software for an operator to dispatch when required, or assembled in
# a dynamic mission or job description and executed when necessary to complete a broader task
# containing these elements.

# For this demo, we will hardcode the information for our particular demo that can be trivially
# updated for another demo or application using an action. This demo is suitable to be run in
# a park, open field, lake, or similar locations.

# This can also be reproduced by another robot at the Persidio parade lawn without modification!

# 1. Go to the location and put the robot somewhere in the middle of the lawn.
# 2. Echo the GPS fix and record the latitude and longitude as the `datum` below.
# 3. Launch RL, drive to waypoints, and record the NavSat odom as the waypoints
#    or determine via math/GUI relative to datum
#    or use GPS fix instead of cartesian, for ROS 2 Iron and newer
###################################################################################################

# Datum to create consistent cartesian reference frame. We picked a measurement near the start.
datum = [37.80046733333333, -122.45829418, 0.0]

# Mission waypoints to be visited in order on a patrol loop.
# Can use absolute GPS (Iron and newer) or cartesian (all distros) relative to datum's origin.
inspection_targets_gps = []
inspection_targets_cartesian = [
    [-32.0, -14.0, 0.0],  # start point (home)
    [-15.0, 11.7, 0.0],  # x, y, yaw
    [2.2, 36.4, 0.0],
    [-15.0, 11.7, 0.0]]


"""
A high-speed GPS patrol loop navigation task
"""


class GPSPatrolDemo(Node):

    def __init__(self):
        super().__init__('gps_waypoint_demo')
        self.stop = False
        self.looped_once = False
        self.demo_thread = None
        self.lock = Lock()

        self.navigator = BasicNavigator()
        self.waitUntilActive()
        time.sleep(2.0)
        self.getParameters()
        self.setRLDatum()

        self.batt_qos = QoSProfile(
            durability=QoSDurabilityPolicy.VOLATILE,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1)
        self.joy_sub = self.create_subscription(
            Joy, 'joy_teleop/joy', self.joyCallback, 10)
        self.batt_sub = self.create_subscription(
            BatteryState, 'platform/bms/state', self.batteryCallback, self.batt_qos)
        print('GPS Waypoint Demo node started.')

    def waitUntilActive(self):
        """Block until the full navigation system is up and running."""
        self.navigator._waitForNodeToActivate('bt_navigator')
        print('Nav2 is ready for use!')

    def getParameters(self):
        # Whether to loop the mission
        self.declare_parameter('loop', True)
        self.loop = self.get_parameter('loop').value

        # Whether to use GPS or cartesian coordinates
        self.declare_parameter('use_gps_coords', False)
        self.use_gps_coords = self.get_parameter('use_gps_coords').value

        # Get demo buttons (square=3, X=0 on PS4)
        self.declare_parameter('start_button', 2)
        self.declare_parameter('exit_button', 0)
        self.start_button = self.get_parameter('start_button').value
        self.exit_button = self.get_parameter('exit_button').value

        # Get minimum battery to exit demo and return to base
        self.declare_parameter('min_battery_lvl', 0.20)
        self.min_battery_lvl = self.get_parameter('min_battery_lvl').value

    def setRLDatum(self):
        global datum
        self.datum_srv = self.create_client(SetDatum, '/datum')
        quaternion = self._quaternion_from_euler(0.0, 0.0, datum[2])
        if self.datum_srv.wait_for_service(timeout_sec=10.0):
            rl_datum = SetDatum.Request()
            rl_datum.geo_pose.position.latitude = datum[0]
            rl_datum.geo_pose.position.longitude = datum[1]
            rl_datum.geo_pose.orientation.z = quaternion[2]
            rl_datum.geo_pose.orientation.w = quaternion[3]
            try:
                future = self.datum_srv.call_async(rl_datum)
                rclpy.spin_until_future_complete(self, future)
                print('Successfully set datum')
            except Exception:
                print('Failed to set datum!')
                exit(1)
        else:
            print('Failed to find set datum service!')
            exit(1)

    def joyCallback(self, msg):
        if msg.buttons[self.exit_button] == 1:
            print('Stop request detected, stopping GPS navigation demo at end of this loop!')
            with self.lock:
                self.stop = True

        if msg.buttons[self.start_button] == 1 and self.demo_thread is None:
            print('Start request detected, starting GPS navigation demo!')
            self.demo_thread = Thread(target=self.runDemo)
            self.demo_thread.daemon = True
            self.demo_thread.start()

    def batteryCallback(self, msg):
        if msg.percentage < self.min_battery_lvl:
            with self.lock:
                self.stop = True

    def runDemo(self):
        while rclpy.ok():
            # Start navigation
            nav_start = self.navigator.get_clock().now()
            if self.use_gps_coords:
                wps = self._wpsToGeoPoses(inspection_targets_gps)
                if self.looped_once:
                    wps.pop(0)
                self.navigator.followGPSWaypoints(wps)
            else:
                wps = self._wpsToPoses(inspection_targets_cartesian)
                if self.looped_once:
                    wps.pop(0)
                self.navigator.followWaypoints(self._wpsToPoses(inspection_targets_cartesian))

            # Track ongoing progress
            i = 0
            while not self.navigator.isTaskComplete() or not rclpy.ok():
                i = i + 1
                feedback = self.navigator.getFeedback()
                if feedback and i % 10 == 0:
                    print('Executing current waypoint: ' + str(feedback.current_waypoint))

                    # Some navigation timeout to demo cancellation
                    if self.navigator.get_clock().now() - nav_start > Duration(seconds=1200.0):
                        self.navigator.cancelTask()

            # Log a result of the loop
            result = self.navigator.getResult()
            if result == TaskResult.SUCCEEDED:
                print('GPS Waypoint demo loop succeeded!')
            elif result == TaskResult.CANCELED:
                print('GPS Waypoint demo loop was canceled!')
            elif result == TaskResult.FAILED:
                print('GPS Waypoint demo loop failed!')
            else:
                print('GPS Waypoint demo loop has an invalid return status!')

            # Check if a stop is requested or no looping is necessary
            with self.lock:
                if self.stop or not self.loop:
                    print('Exiting GPS Waypoint demo. '
                          'Stop was requested or looping was not enabled.')
                    return

            self.looped_once = True

    def _wpsToPoses(self, wps):
        poses = []
        for wp in wps:
            pose = PoseStamped()
            pose.header.frame_id = 'map'
            pose.pose.position.x = wp[0]
            pose.pose.position.y = wp[1]
            quaternion = self._quaternion_from_euler(0.0, 0.0, wp[2])
            pose.pose.orientation.z = quaternion[2]
            pose.pose.orientation.w = quaternion[3]
            poses.append(pose)

        # Add starting point for full loop
        poses.append(copy.deepcopy(poses[0]))
        return poses

    def _wpsToGeoPoses(self, wps):
        poses = []
        for wp in wps:
            pose = GeoPose()
            pose.pose.position.latitude = wp[0]
            pose.pose.position.longitude = wp[1]
            quaternion = self._quaternion_from_euler(0.0, 0.0, wp[2])
            pose.pose.orientation.z = quaternion[2]
            pose.pose.orientation.w = quaternion[3]
            poses.append(pose)

        # Add starting point for full loop
        poses.append(copy.deepcopy(poses[0]))
        return poses

    def _quaternion_from_euler(self, roll, pitch, yaw):
        qx = np.sin(roll/2) * np.cos(pitch/2) * np.cos(yaw/2) - \
            np.cos(roll/2) * np.sin(pitch/2) * np.sin(yaw/2)
        qy = np.cos(roll/2) * np.sin(pitch/2) * np.cos(yaw/2) + \
            np.sin(roll/2) * np.cos(pitch/2) * np.sin(yaw/2)
        qz = np.cos(roll/2) * np.cos(pitch/2) * np.sin(yaw/2) - \
            np.sin(roll/2) * np.sin(pitch/2) * np.cos(yaw/2)
        qw = np.cos(roll/2) * np.cos(pitch/2) * np.cos(yaw/2) + \
            np.sin(roll/2) * np.sin(pitch/2) * np.sin(yaw/2)
        return [qx, qy, qz, qw]


def main():
    rclpy.init()
    node = GPSPatrolDemo()
    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    executor.spin()
    exit(0)


if __name__ == '__main__':
    main()
