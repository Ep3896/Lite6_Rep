#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from simple_pid import PID
from geometry_msgs.msg import Pose, Point, PointStamped
from visualization_msgs.msg import MarkerArray
from std_msgs.msg import Float32MultiArray, Bool
from lite6_enrico_interfaces.action import GoToPose
from tf2_ros import Buffer, TransformListener, LookupException, ConnectivityException, ExtrapolationException
import tf2_geometry_msgs.tf2_geometry_msgs
import math

# PID gains
KP = 0.01
KI = 0.0
KD = 0.01

# Thresholds
ERROR_THRESHOLD = 0.02  # Meters
Z_THRESHOLD = 0.20  # Meters
MOVEMENT_THRESHOLD = 0.01  # Maximum allowed movement in meters


# Camera controller class
class CameraController:

    def __init__(self):
        self.position = Point(x=0.0, y=0.0, z=0.0)
        # PID controllers for x, y, z
        self.pid_x = PID(KP, KI, KD)
        self.pid_y = PID(KP, KI, KD)
        self.pid_z = PID(KP, KI, KD)
        # Set PID's update rate
        self.pid_x.sample_time = 0.0333
        self.pid_y.sample_time = 0.0333
        self.pid_z.sample_time = 0.0333
        # Control clip value
        self.clip_val = 0.1

    def set_new_goal(self, target_position: Point):
        # Reset internal pid variables and set new goal position, starting from current one
        # setpoint is the target position, so the goal that I want to send the robot to
        self.pid_x.setpoint = target_position.x
        self.pid_y.setpoint = target_position.y
        self.pid_z.setpoint = target_position.z

    def get_position_error(self):
        return abs(self.position.x - self.pid_x.setpoint), abs(self.position.y - self.pid_y.setpoint), abs(self.position.z - self.pid_z.setpoint)

    def update_position(self):
        # Compute velocity control based on current joint position
        velocity_x = self.pid_x(self.position.x)
        velocity_y = self.pid_y(self.position.y)
        velocity_z = self.pid_z(self.position.z)

        # Saturate control value
        velocity_x = max(min(velocity_x, self.clip_val), -self.clip_val)
        velocity_y = max(min(velocity_y, self.clip_val), -self.clip_val)
        velocity_z = max(min(velocity_z, self.clip_val), -self.clip_val)

        # Update current position
        self.position.x += velocity_x * self.pid_x.sample_time
        self.position.y += velocity_y * self.pid_y.sample_time
        self.position.z += velocity_z * self.pid_z.sample_time

    def log(self):
        return "Camera - current_position: ({0}, {1}, {2})".format(self.position.x, self.position.y, self.position.z)


# Main control node class
class ControllerNode(Node):

    def __init__(self):
        super().__init__("camera_control_node")

        # Internal controller for the camera
        self.camera_controller = CameraController()

        # Subscriber for desired goal
        self.create_subscription(MarkerArray, '/yolo/dgb_bb_markers', self.detection_callback, 10)

        # Publisher for goal coordinates
        self.goal_publisher = self.create_publisher(Float32MultiArray, 'goal_coordinates', 10)

        # Action client for sending goal to the action server
        self._action_client = ActionClient(self, GoToPose, 'go_to_pose')

        # TF2 buffer and listener
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # Variales for control loop
        self.camera_controller_timer = None

        self.get_logger().info("Camera control node up and running!")

    def detection_callback(self, msg: MarkerArray):
        if msg.markers:
            marker = msg.markers[0]  # Process the first marker, or modify as needed
            bbox_center = PointStamped()
            bbox_center.header.frame_id = 'camera_depth_frame'
            bbox_center.header.stamp = self.get_clock().now().to_msg()
            bbox_center.point = marker.pose.position

            # Transform the point to the world frame
            try:
                transform = self.tf_buffer.lookup_transform('world', 'camera_depth_frame', rclpy.time.Time(), rclpy.duration.Duration(seconds=1))
                world_point = tf2_geometry_msgs.do_transform_point(bbox_center, transform).point

                self.get_logger().info(
                    "Received marker position: [x: {}, y: {}, z: {}]".format(world_point.x, world_point.y, world_point.z))

                # Set current and target positions for the camera controller
                self.camera_controller.set_new_goal(world_point)
                # Start timer for control cycle
                self.camera_controller_timer = self.create_timer(self.camera_controller.pid_x.sample_time, self.control_loop)

            except (LookupException, ConnectivityException, ExtrapolationException) as e:
                self.get_logger().error(f'Could not transform point: {e}')

    def control_loop(self):
        # Stop timer if position errors fall below threshold
        err_x, err_y, err_z = self.camera_controller.get_position_error()
        if abs(err_x) < ERROR_THRESHOLD and abs(err_y) < ERROR_THRESHOLD and abs(err_z) < Z_THRESHOLD:
            self.camera_controller_timer.cancel()
            self.get_logger().info("Reached target position")
            return

        # Update camera position according to control law
        self.camera_controller.update_position()
        self.get_logger().info(self.camera_controller.log())

        # Build and publish updated goal message
        goal_x = self.camera_controller.position.x
        goal_y = self.camera_controller.position.y
        goal_z = self.camera_controller.position.z
        self.get_logger().info(f'Goal: ({goal_x}, {goal_y}, {goal_z})')

        # Publish the goal coordinates
        goal_msg = Float32MultiArray()
        goal_msg.data = [goal_x, goal_y, goal_z]
        self.goal_publisher.publish(goal_msg)

        # Send the new goal to the action server
        self.send_goal(goal_x, goal_y, goal_z)

    def send_goal(self, x, y, z):
        goal_msg = GoToPose.Goal()
        goal_msg.pose.position.x = x 
        goal_msg.pose.position.y = y 
        goal_msg.pose.position.z = z 
        goal_msg.pose.orientation.w = 1.0  # Assuming a default orientation

        print("Waiting for server")
        self._action_client.wait_for_server(timeout_sec=1.0)
        print("Sending goal")
        self._send_goal_future = self._action_client.send_goal_async(goal_msg)
        self._send_goal_future.add_done_callback(self.goal_response_callback)

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('Goal rejected :(')
            return

        self.get_logger().info('Goal accepted :)')

        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info(f'Result: {result}')
        if result.success:
            self.get_logger().info('Goal succeeded!')
        else:
            self.get_logger().info('Goal failed!')


def main(args=None):
    rclpy.init(args=args)
    controller_node = ControllerNode()
    # Spin indefinitely
    rclpy.spin(controller_node)
    # On shutdown...
    controller_node.destroy_node()
    rclpy.shutdown()

# Script entry point
if __name__ == "__main__":
    main()
