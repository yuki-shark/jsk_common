#!/usr/bin/env python

import rospy
from jsk_network_tools.msg import FC2OCSLargeData
from jsk_network_tools.silverhammer_util import *
from threading import Lock
from StringIO import StringIO
from std_msgs.msg import Time
from io import BytesIO
from socket import *
from struct import pack
import diagnostic_updater
from diagnostic_msgs.msg import DiagnosticStatus
import roslib
from roslib.message import get_message_class

#ifdef USE_THREAD
# from threading import Thread
# from Queue import Queue
#else
from multiprocessing import Process, Queue
#endif

def receive_process_func(packets_queue, receive_ip, receive_port, packet_size, message_class, topic_prefix, pesimistic, fragment_packets_torelance):
    # create socket
    socket_server = socket(AF_INET, SOCK_DGRAM)
    socket_server.setsockopt(SOL_SOCKET, SO_RCVBUF, 1564475392)
    recv_buffer_size = socket_server.getsockopt(SOL_SOCKET, SO_RCVBUF)
    rospy.loginfo("kernel receive socket buffer: %d" % recv_buffer_size)
    if recv_buffer_size < 1564475392:
        rospy.logwarn("kernel receive socket buffer must be at least 1564475392 bytes.")
        rospy.logwarn("to change this value, execute:")
        rospy.logwarn("sudo sysctl -w net.core.rmem_max=4259840")
    rospy.logwarn("try to bind %s:%d" % (receive_ip, receive_port))
    socket_server.bind((receive_ip, receive_port))

    packets = {}
    last_received_data_seq_id = -1

    while not rospy.is_shutdown():
        recv_data, addr = socket_server.recvfrom(packet_size)
        packet = LargeDataUDPPacket.fromData(recv_data, packet_size)
        if packet.seq_id in packets:
            packets[packet.seq_id] += [packet]
        else:
            packets[packet.seq_id] = [packet]
        last_received_data_seq_id = packet.seq_id

        # enqueue complete packets
        for seq_id, pcts in packets.items():
            if pcts[0].num == len(pcts):
                packets_queue.put(packets.pop(seq_id))

        # prune incomplete packets
        for seq_id, pcts in packets.items():
            if seq_id < last_received_data_seq_id - fragment_packets_torelance and len(pcts) != pcts[0].num:
                if pesimistic:
                    rospy.logwarn("packets(seq: %d, recv: %d/%d) are pruned." % (seq_id, len(pcts), pcts[0].num))
                    del packets[seq_id]
                else:
                    rospy.logwarn("packets(seq: %d, recv: %d/%d) are incomplete data." % (seq_id, len(pcts), pcts[0].num))
                    packets_queue.put(packets.pop(seq_id))


class SilverHammerReceiver:
    def __init__(self):
        message_class_str = rospy.get_param("~message", 
                                            "jsk_network_tools/FC2OCSLargeData")
        try:
            self.message_class = get_message_class(message_class_str)
        except:
            raise Exception("invalid topic type: %s"%message_class_str)
        self.lock = Lock()
        self.diagnostic_updater = diagnostic_updater.Updater()
        self.diagnostic_updater.setHardwareID("none")
        self.diagnostic_updater.add("HighspeedReceiver", self.diagnosticCallback)
        self.latch = rospy.get_param("~latch", True)
        self.pesimistic = rospy.get_param("~pesimistic", False)
        self.fragment_packets_torelance = rospy.get_param("~fragment_packets_torelance", 20)
        self.receive_port = rospy.get_param("~receive_port", 16484)
        self.receive_ip = rospy.get_param("~receive_ip", "localhost")
        self.topic_prefix = rospy.get_param("~topic_prefix", "/from_fc")
        if not self.topic_prefix.startswith("/"):
            self.topic_prefix = "/" + self.topic_prefix
        if self.topic_prefix == "/":
            self.topic_prefix = ""
        self.publishers = publishersFromMessage(self.message_class,
                                                self.topic_prefix, 
                                                latch=self.latch)
        self.packet_size = rospy.get_param("~packet_size", 1400)   #2Hz
        self.launched_time = rospy.Time.now()
        self.last_received_time = rospy.Time(0)
        self.last_received_time_pub = rospy.Publisher("~last_received_time", Time)
        self.last_published_seq_id = -1
        self.diagnostic_timer = rospy.Timer(rospy.Duration(1.0 / 10),
                                            self.diagnosticTimerCallback)
        self.packets_queue = Queue()
#ifdef USE_THREAD
#        self.receive_process = Thread(target=receive_process_func, args=(self.packets_queue,
#else
        self.receive_process = Process(target=receive_process_func, args=(self.packets_queue,
#endif
                                                                          self.receive_ip,
                                                                          self.receive_port,
                                                                          self.packet_size,
                                                                          self.message_class,
                                                                          self.topic_prefix,
                                                                          self.pesimistic,
                                                                          self.fragment_packets_torelance))
    def diagnosticCallback(self, stat):
        # always OK
        stat.summary(DiagnosticStatus.OK, "OK")
        with self.lock:
            now = rospy.Time.now()
            stat.add("Uptime [sec]",
                     (now - self.launched_time).to_sec())
            stat.add("Time from last input [sec]", 
                     (now - self.last_received_time).to_sec())
            stat.add("UDP address", self.receive_ip)
            stat.add("UDP port", self.receive_port)
        return stat
    def diagnosticTimerCallback(self, event):
        self.diagnostic_updater.update()
        with self.lock:
            self.last_received_time_pub.publish(self.last_received_time)
    def run(self):
        self.receive_process.start()
        while not rospy.is_shutdown():
            rospy.loginfo("%d packets are in queue" % self.packets_queue.qsize())
            packets = self.packets_queue.get()
            packets.sort(key=lambda p: p.id)
            self.last_received_time = rospy.Time.now()
            try:
                self.concatenatePackets(packets)
            except Exception,e:
                rospy.logerr("failed to concatenate packets: %s", e.message)
        else:
            self.receive_process.join()

    def concatenatePackets(self, packets):
        if packets[0].seq_id < self.last_published_seq_id:
            rospy.logwarn("publishing out-of-ordered packets %d -> %d" % (self.last_published_seq_id, packets[0].seq_id))
        self.last_published_seq_id = packets[0].seq_id
        packet_data_length = len(packets[0].data)
        packet_index = 0
        b = StringIO()
        for i in range(packets[0].num):
            if packets[packet_index].id == i:
                packet = packets[packet_index]
                b.write(packet.data)
                packet_index = packet_index + 1
            else:
                # fill by dummy data
                b.write(chr(0) * packet_data_length)
        deserialized_data = []
        rospy.msg.deserialize_messages(b, deserialized_data,
                                       self.message_class)
        rospy.loginfo("received %d message" % len(deserialized_data))
        if len(deserialized_data) > 0:
            # publish data
            msg = deserialized_data[0]
            messages = decomposeLargeMessage(msg, self.topic_prefix)
            for pub in self.publishers:
                if pub.name in messages:
                    rospy.loginfo("publishing %s" % pub.name)
                    pub.publish(messages[pub.name])
                else:
                    rospy.logwarn("""cannot find '%s' in deserialized messages %s""" % (pub.name, messages.keys()))
        else:
            rospy.logerr("missed some packets")


if __name__ == "__main__":
    rospy.init_node("silverhammer_highspeed_receiver")
    receiver = SilverHammerReceiver()
    receiver.run()
    
