#!/usr/bin/env python
import pika
import sys
import time
import subprocess
import random
import threading
import requests
import json
import signal

from command_args import get_args, get_mandatory_arg, get_optional_arg
from RabbitPublisher import RabbitPublisher
from MultiTopicConsumer import MultiTopicConsumer
from QueueStats import QueueStats
from ChaosExecutor import ChaosExecutor
from printer import console_out
from MessageMonitor import MessageMonitor
from ConsumerManager import ConsumerManager
from BrokerManager import BrokerManager

stop_please = False
stop_requests = 0

def interuppt_handler(signum, frame):
    global stop_please, stop_requests
    console_out("STOP REQUESTED", "TEST RUNNER")
    stop_please = True
    stop_requests +=1

    if stop_requests >= 2:
        sys.exit(-2) 
    

def main():

    #signal.signal(signal.SIGINT, interuppt_handler)
    args = get_args(sys.argv)

    count = -1 # no limit
    tests = int(get_mandatory_arg(args, "--tests"))
    run_minutes = int(get_mandatory_arg(args, "--run-minutes"))
    consumer_count = int(get_mandatory_arg(args, "--consumers"))
    grace_period_sec = int(get_mandatory_arg(args, "--grace-period-sec"))
    queue = get_mandatory_arg(args, "--queue")
    queue_type = get_mandatory_arg(args, "--queue-type")
    sac = get_mandatory_arg(args, "--sac")

    publisher_count = int(get_optional_arg(args, "--publishers", "1"))
    print_mod = int(get_optional_arg(args, "--print-mod", "0"))
    new_cluster = get_optional_arg(args, "--new-cluster", "true")
    in_flight_max = int(get_optional_arg(args, "--in-flight-max", "10"))
    sequence_count = int(get_optional_arg(args, "--sequences", "1"))
    cluster_size = get_optional_arg(args, "--cluster", "3")
    chaos = get_optional_arg(args, "--chaos-actions", "true")
    chaos_mode = get_optional_arg(args, "--chaos-mode", "mixed")
    chaos_min_interval = int(get_optional_arg(args, "--chaos-min-interval", "60"))
    chaos_max_interval = int(get_optional_arg(args, "--chaos-max-interval", "120"))
    consumer_actions = get_optional_arg(args, "--consumer-actions", "true")
    con_action_min_interval = int(get_optional_arg(args, "--consumer-min-interval", "20"))
    con_action_max_interval = int(get_optional_arg(args, "--consumer-max-interval", "60"))

    if print_mod == 0:
        print_mod = in_flight_max * 5

    include_chaos = True
    if chaos.upper() == "FALSE":
        include_chaos = False

    include_con_actions = True
    if consumer_actions.upper() == "FALSE":
        include_con_actions = False

    sac_enabled = True
    if sac.upper() == "FALSE":
        sac_enabled = False

    message_type = "sequence"
    
    for test_number in range(tests):

        print("")
        console_out(f"TEST RUN: {str(test_number)} --------------------------", "TEST RUNNER")
        if new_cluster.upper() == "TRUE":
            subprocess.call(["bash", "../automated/setup-test-run.sh", cluster_size, "3.8"])
            console_out(f"Waiting for cluster...", "TEST RUNNER")
            time.sleep(30)

        console_out(f"Cluster status:", "TEST RUNNER")
        subprocess.call(["bash", "../cluster/cluster-status.sh"])
        
        broker_manager = BrokerManager()
        broker_manager.load_initial_nodes()
        initial_nodes = broker_manager.get_initial_nodes()
        console_out(f"Initial nodes: {initial_nodes}", "TEST RUNNER")

        queue_name = queue + "_" + str(test_number)
        mgmt_node = broker_manager.get_random_init_node()
        queue_created = False

        while queue_created == False:  
            if sac_enabled:  
                queue_created = broker_manager.create_sac_queue(mgmt_node, queue_name, cluster_size, queue_type)
            else:
                queue_created = broker_manager.create_queue(mgmt_node, queue_name, cluster_size, queue_type)

            if queue_created == False:
                time.sleep(5)

        time.sleep(10)

        msg_monitor = MessageMonitor(print_mod)
        stats = QueueStats('jack', 'jack', queue_name)
        chaos = ChaosExecutor(initial_nodes)

        if chaos_mode == "partitions":
            chaos.only_partitions()
        elif chaos_mode == "nodes":
            chaos.only_kill_nodes()

        consumer_manager = ConsumerManager(broker_manager, msg_monitor, "TEST RUNNER")

        pub_node = broker_manager.get_random_init_node()
        publisher = RabbitPublisher(f"PUBLISHER(Test:{test_number} Id:P1)", initial_nodes, pub_node, in_flight_max, 120, print_mod)
        consumer_manager.add_consumers(consumer_count, test_number, queue_name)

        monitor_thread = threading.Thread(target=msg_monitor.process_messages)
        monitor_thread.start()
        
        consumer_manager.start_consumers()

        if publisher_count == 1:
            pub_thread = threading.Thread(target=publisher.publish_direct,args=(queue_name, count, sequence_count, 0, "sequence"))
            pub_thread.start()
            console_out("publisher started", "TEST RUNNER")

        if include_con_actions or include_chaos:
            init_wait_sec = 20
            console_out(f"Will start chaos and consumer actions in {init_wait_sec} seconds", "TEST RUNNER")
            time.sleep(init_wait_sec)

        if include_chaos:
            chaos_thread = threading.Thread(target=chaos.start_random_single_action_and_repair,args=(chaos_min_interval,chaos_max_interval))
            chaos_thread.start()
            console_out("Chaos executor started", "TEST RUNNER")

        if include_con_actions:
            consumer_action_thread = threading.Thread(target=consumer_manager.start_random_consumer_actions,args=(con_action_min_interval, con_action_max_interval))
            consumer_action_thread.start()
            console_out("Consumer actions started", "TEST RUNNER")

        
        ctr = 0
        run_seconds = run_minutes * 60
        while ctr < run_seconds and not stop_please:
            try:
                time.sleep(1)
                ctr += 1

                if ctr % 60 == 0:
                    console_out(f"Test at {int(ctr/60)} minute mark, {int((run_seconds-ctr)/60)} minutes left", "TEST RUNNER")
            except KeyboardInterrupt:
                console_out(f"Test forced to stop at {int(ctr/60)} minute mark, {int((run_seconds-ctr)/60)} minutes left)", "TEST RUNNER")
                break

        try:
            chaos.stop_random_single_action_and_repair()
            consumer_manager.stop_random_consumer_actions()
            
            if include_chaos:
                chaos_thread.join()

            if include_con_actions:
                consumer_action_thread.join()
        except Exception as e:
            console_out("Failed to stop chaos cleanly: " + str(e), "TEST RUNNER")

        console_out("Resuming consumers", "TEST RUNNER")
        consumer_manager.resume_all_consumers()
        
        if publisher_count == 1:
            publisher.stop(True)

        console_out("starting grace period for consumer to catch up", "TEST RUNNER")
        ctr = 0
        
        while ctr < grace_period_sec:
            if msg_monitor.get_unique_count() >= publisher.get_pos_ack_count() and len(publisher.get_msg_set().difference(msg_monitor.get_msg_set())) == 0:
                break
            time.sleep(1)
            ctr += 1

        confirmed_set = publisher.get_msg_set()
        not_consumed_msgs = confirmed_set.difference(msg_monitor.get_msg_set())

        console_out("RESULTS ----------------------------------------", "TEST RUNNER")
        console_out(f"Confirmed count: {publisher.get_pos_ack_count()} Received count: {msg_monitor.get_receive_count()} Unique received: {msg_monitor.get_unique_count()}", "TEST RUNNER")

        success = True
        if len(not_consumed_msgs) > 0:
            console_out(f"FAILED TEST: Potential failure to promote Waiting to Active. Not consumed count: {len(not_consumed_msgs)}", "TEST RUNNER")
            success = False

        if msg_monitor.get_out_of_order() == True:
            success = False
            console_out(f"FAILED TEST: Received out-of-order messages", "TEST RUNNER")

        if success:
            console_out("TEST OK", "TEST RUNNER")

        console_out("RESULTS END ------------------------------------", "TEST RUNNER")

        try:
            consumer_manager.stop_all_consumers()
            
            if publisher_count == 1:
                pub_thread.join()
            msg_monitor.stop_consuming()
            monitor_thread.join()
        except Exception as e:
            console_out("Failed to clean up test correctly: " + str(e), "TEST RUNNER")

        console_out(f"TEST {str(test_number )} COMPLETE", "TEST RUNNER")

if __name__ == '__main__':
    main()