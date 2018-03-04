#!/usr/bin/env python

import sys
import rospy
import math
import tf
import copy

import numpy as np
from nav_msgs.srv import GetMap
from comp313p_reactive_planner_controller.occupancy_grid import OccupancyGrid
from comp313p_reactive_planner_controller.grid_drawer import OccupancyGridDrawer
from comp313p_reactive_planner_controller.cell import CellLabel
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Odometry
from threading import Lock
from geometry_msgs.msg  import Twist
from comp313p_mapper.msg import MapUpdate
from comp313p_mapper.srv import ChangeMapperState
from bresenhamalgorithm import bresenham

# This class implements basic mapping capabilities. Given knowledge
# about the robot's position and orientation, it processes laser scans
# to produce a new occupancy grid. If this grid differs from the
# previous one, a new grid is created and broadcast.

class MapperNode(object):

    def __init__(self):

        rospy.sleep(4)

        # Get the ground truth map from stdr; we use this to figure out the dimensions of the map       
        rospy.init_node('mapper_node', anonymous=True)
        self.mapServer = rospy.ServiceProxy('static_map', GetMap)
        resp = self.mapServer()

        # Create the publisher
        self.mapUpdatePublisher = rospy.Publisher('updated_map', MapUpdate, queue_size = 1)
        

        # Get the map scale
        mapScale = rospy.get_param('plan_scale',5)

        # Create the occupancy grid map. This is the "raw" one from the sensor.
        self.occupancyGrid = OccupancyGrid(resp.map.info.width, resp.map.info.height, resp.map.info.resolution, 0.5)
	self.occupancyGrid.setScale(mapScale)
	self.occupancyGrid.scaleEmptyMap()
                         
        # Create the delta occupancy grid map. This stores the difference since the last time the map was sent out
        self.deltaOccupancyGrid = OccupancyGrid(resp.map.info.width, resp.map.info.height, resp.map.info.resolution, 0)
	self.deltaOccupancyGrid.setScale(mapScale)
	self.deltaOccupancyGrid.scaleEmptyMap()

        # Create the show delta occupancy grid map. This stores the difference since the last time the map was sent out.
        # Done as a cheesy way to sort out threading issues
        self.showDeltaOccupancyGrid = OccupancyGrid(resp.map.info.width, resp.map.info.height, resp.map.info.resolution, 0)
	self.showDeltaOccupancyGrid.setScale(mapScale)
	self.showDeltaOccupancyGrid.scaleEmptyMap()

        windowHeight = rospy.get_param('maximum_window_height_in_pixels', 600)
        
        self.occupancyGridDrawer = OccupancyGridDrawer('Mapper Node Occupancy Grid',\
                                                       self.occupancyGrid, windowHeight)
	self.occupancyGridDrawer.open()
        self.deltaOccupancyGridDrawer = OccupancyGridDrawer('Mapper Node Delta Occupancy Grid',\
                                                            self.showDeltaOccupancyGrid, windowHeight)
	self.deltaOccupancyGridDrawer.open()
	rospy.loginfo('------> 4')

        # Set up the subscribers. These track the robot position, speed and laser scans.
        self.mostRecentOdometry = Odometry()
        self.odometrySubscriber = rospy.Subscriber("robot0/odom", Odometry, self.odometryCallback, queue_size=1)
        self.mostRecentTwist = Twist();
        self.twistSubscriber = rospy.Subscriber('/robot0/cmd_vel', Twist, self.twistCallback, queue_size=1)
        self.laserSubscriber = rospy.Subscriber("robot0/laser_0", LaserScan, self.laserScanCallback, queue_size=1)

        # Flag set to true if graphics can be updated
        self.updateVisualisation = False

        # Set up the lock to ensure thread safety
        self.dataCopyLock = Lock()

        self.noOdometryReceived = True
        self.noTwistReceived = True


        self.service = rospy.Service('change_mapping_state', ChangeMapperState, self.mappingStateService)

        self.enableMapping = rospy.get_param('start_with_mapping_enabled', True)

        rospy.loginfo('------> Initialised')

    def odometryCallback(self, msg):
        self.dataCopyLock.acquire()
        self.mostRecentOdometry = msg
        self.noOdometryReceived = False
        self.dataCopyLock.release()

    def twistCallback(self, msg):
        self.dataCopyLock.acquire()
        self.mostRecentVelocity = msg
        self.noTwistReceived = False
        self.dataCopyLock.release()

    def mappingStateService(self, changeMappingState):
        self.enableMapping = changeMappingState.enableMapping

    # Handle the laser scan callback. First process the scans and update the various maps
    
    def laserScanCallback(self, msg):

        # Can't process anything until stuff is enabled
        if self.enableMapping is False:
            return

        # Can't process anything until we have the first scan
        if (self.noOdometryReceived is True) or (self.noTwistReceived is True):
            return

        # Process the scan
        gridHasChanged = self.parseScan(msg)

        # If nothing has changed, return
        if gridHasChanged is False:
            return

        self.updateVisualisation = True

        # Construct the map update message
        mapUpdateMessage = MapUpdate()

        mapUpdateMessage.header.stamp = rospy.Time().now()
        mapUpdateMessage.scale = self.occupancyGrid.getScale()
        mapUpdateMessage.extentInCells = self.occupancyGrid.getExtentInCells()
        mapUpdateMessage.extent = self.occupancyGrid.getExtent()
        mapUpdateMessage.occupancyGrid = copy.deepcopy(self.occupancyGrid)
        mapUpdateMessage.deltaOccupancyGrid = copy.deepcopy(self.deltaOccupancyGrid)

        # Send it out
        self.mapUpdatePublisher.publish(mapUpdateMessage)
        
    # Predict the pose of the robot to the current time. This is to
    # hopefully make the pose of the robot a bit more accurate. The
    # equation is: currentPose = lastPose + dT * lastTwist. Note this
    # isn't quite right. e.g. a more proper implementation would store
    # a history of velocities and interpolate over them.
    
    def predictPose(self, predictTime):

        # Copy the last odometry and velocity
        self.dataCopyLock.acquire()
        currentPose = copy.deepcopy(self.mostRecentOdometry.pose.pose)
        currentPoseTime = self.mostRecentOdometry.header.stamp.to_sec()
        currentTwist = copy.deepcopy(self.mostRecentTwist)
        self.dataCopyLock.release()

        dT = predictTime - currentPoseTime

        quaternion = (currentPose.orientation.x, currentPose.orientation.y,
                      currentPose.orientation.z, currentPose.orientation.w)
        euler = tf.transformations.euler_from_quaternion(quaternion)

        theta = euler[2]

        # These are the "ideal motion model" prediction equations from
        # stdr which attempt to accurately describe the trajectory of
        # the robot if it turns as it moves. The equations are precise
        # if the angular and linear velocity is constant over the
        # prediction interval.

        if (abs(currentTwist.angular.z) < 1e-6):
            x = currentPose.position.x + dT * currentTwist.linear.x * math.cos(theta)
            y = currentPose.position.y + dT * currentTwist.linear.x * math.sin(theta)
        else:
            x = currentPose.position.x - currentTwist.linear.x / currentTwist.angular.z * sin(theta) + \
                currentTwist.linear.x / currentTwist.angular.z * sin(theta + dT * currentTwist.angular.z)

            y = currentPose.position.y - currentTwist.linear.x / currentTwist.angular.z * cos(theta) + \
                currentTwist.linear.x / currentTwist.angular.z * cos(theta + dT * currentTwist.angular.z);

        theta = theta + currentTwist.angular.z * dT

        tooFast = abs(currentTwist.linear.x) > 4
        
        return x, y, theta, tooFast

    def parseScan(self, msg):

        # Predict the robot pose to the time the scan was taken
        x, y, theta, tooFast = self.predictPose(msg.header.stamp.to_sec())

        # If the robot is travelling too quickly, don't bother trying to analyse the scan
        if tooFast is True:
            print 'Too Fast'
            return
        
        # Clear the flag which shows that the map has changed
        gridHasChanged = False

        # Clear the delta map, to imply that no changes have been detected
        self.deltaOccupancyGrid.clearMap(0)
        
        # For each ray, check the range is good. If so, check all the
        # cells along the ray and mark cells as either open or
        # blocked. To get around numerical issues, we trace along each
        # ray in turn and terminate when we hit the first obstacle or at the end of the ray.
        for ii in range(int(math.floor((msg.angle_max - msg.angle_min) / msg.angle_increment))):
            # rospy.loginfo("{} {} {}".format(msg.ranges[ii],msg.angle_min,msg.angle_max))

            detectedRange = msg.ranges[ii]
            
            # If the detection is below the minimum range, assume this ray is busted and continue
            if (detectedRange <= msg.range_min):
                continue

            rayEndsOnObject = True

            # If the detection range is beyond the end of the sensor,
            # this is the mark which says that nothing was detected
            if detectedRange >= msg.range_max:
                rayEndsOnObject = False
                detectedRange = msg.range_max

            # Get the angle of this ray
            angle = msg.angle_min + msg.angle_increment * ii + theta

            # Get the list of cells which sit on this ray. The ray is
            # scaled so that the last sell is at the detected range
            # from the sensor.
            between = self.ray_trace(detectedRange, x, y, angle, msg)

            # If between is empty, something went wrong with the ray cast, so skip
            if len(between) == 0:
                continue

            # Traverse along the ray and set cells. We can only change
            # cells from unknown (0.5) to free. If we encounter a
            # blocked cell, terminate. Sometimes the ray can slightly
            # extend through a blocked cell due to numerical rounding
            # issues.
            traversedToEnd = True
            for point in between:
                try:
                    if self.occupancyGrid.getCell(point[0], point[1]) > 0.5:
                        traversedToEnd = False
                        break
                    
                    if self.occupancyGrid.getCell(point[0], point[1]) == 0.5:
                        self.occupancyGrid.setCell(point[0], point[1], 0)
                        self.deltaOccupancyGrid.setCell(point[0], point[1], 1.0)
                        self.showDeltaOccupancyGrid.setCell(point[0], point[1], 1.0)
                        gridHasChanged = True
                except IndexError as e:
                    print(e)
                    print "between: " + str(point[0]) + ", " + str(point[1])
                    print "extent: " + str(self.occupancyGrid.getExtent())

            # If we got to the end okay, see if we have to mark the
            # state of the end cell to occupied or not. To do this, we 

            # Note that we can change a cell
            # from unknown and free to occupied, but we cannot change
            # the state from occupied back to anything else. This gets
            # around the issue that there can be "blinking" between
            # whether a cell is occupied or not.
            if (traversedToEnd is True) & (rayEndsOnObject is True):
                lastPoint = between[-1]
                if self.occupancyGrid.getCell(lastPoint[0], lastPoint[1]) < 1.0:
                    self.occupancyGrid.setCell(lastPoint[0], lastPoint[1], 1)
                    self.deltaOccupancyGrid.setCell(lastPoint[0], lastPoint[1], 1.0)
                    self.showDeltaOccupancyGrid.setCell(lastPoint[0], lastPoint[1], 1.0)
                    gridHasChanged = True

        return gridHasChanged

    def ray_trace(self, dist, x, y, angle, scanmsg):
        """
        Function to get a list of points between two points
        :param origin: position of the origin in world coordinates
        :param dist: distance to end point
        :param angle: angle from robot
        :param scanmsg: Laser Scan message
        :return: list of points in between the origin and end point
        """
        startPoint = self.occupancyGrid.getCellCoordinatesFromWorldCoordinates([math.cos(angle) * scanmsg.range_min + x, \
                                                                              math.sin(angle) * scanmsg.range_min + y])
        endPoint = self.occupancyGrid.getCellCoordinatesFromWorldCoordinates([math.cos(angle) * dist + x, \
                                                                              math.sin(angle) * dist + y])

        #pointsOld = self.ray_trace_old(dist, x, y, angle, scanmsg)

        points = bresenham(endPoint, startPoint)
        #print str(startPoint) + ':' + str(points.path[0])
        #print str(dist)
        #print str(endPoint) + ':' + str(startPoint)
        #print str(endPoint) + ':' + str(points.path[-1])
        
        #assert startPoint == points.path[0]
        #assert endPoint == points.path[-1]

        return points.path

    def ray_trace_old(self, dist, x, y, angle, scanmsg):

        points = []

        space = np.linspace(scanmsg.range_min, dist, scanmsg.range_max * 5)
        for a in space:
            point_world_coo = [math.cos(angle) * a + x,
                               math.sin(angle) * a + y]
            points.append(self.occupancyGrid.getCellCoordinatesFromWorldCoordinates(point_world_coo))


        print 'old:' + str(points[0]) + '=>' +  str(points[-1])


        return points


    
    def update_visualisation(self):

        if self.updateVisualisation is False:
            return
        
	self.occupancyGridDrawer.update()
	self.deltaOccupancyGridDrawer.update()
        self.showDeltaOccupancyGrid.clearMap(0)
        self.updateVisualisation = False
	
    def run(self):
        while not rospy.is_shutdown():
            rospy.sleep(2)
            self.update_visualisation()
        
  

  
