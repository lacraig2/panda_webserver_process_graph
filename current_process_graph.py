#!/usr/bin/env python3
from panda import Panda, ffi, blocking
from panda.x86.helper import dump_regs, registers
from datetime import datetime, timedelta

from flask import Flask, request, render_template
import threading
from graphviz import Graph
import base64

# Start with a basic flask app webpage.
from flask_socketio import SocketIO, emit
from flask import Flask, render_template, url_for, copy_current_request_context
from random import random
from time import sleep
from threading import Thread, Event

app = Flask(__name__)
app.use_reloader = False
app.debug = False
app.config['SECRET_KEY'] = 'secret!'
app.config['DEBUG'] = True

#turn the flask app into a socketio app
socketio = SocketIO(app, async_mode=None, logger=True, engineio_logger=True, debug=False, use_reloader=False)

panda = Panda("x86_64", mem="1G",qcow="bionic-server-cloudimg-amd64-noaslr-nokaslr.qcow2",
            expect_prompt=rb"root@ubuntu:.*",
            extra_args=["-nographic",                           
            "-net", "nic,netdev=net0",
            "-netdev", "user,id=net0,",  
            ])

panda.set_os_name("linux-64-ubuntu:4.15.0-72-generic-noaslr-nokaslr")


class Process(object):
    def __init__(self, proc_object):
        self.pid = proc_object.pid
        self.ppid = proc_object.ppid
        self.start_time = proc_object.create_time
        try:
            self.name = ffi.string(proc_object.name).decode()
        except:
            self.name = "?"
        self.children = set()
        self.parent = None
    
    @property
    def depth(self):
        if hasattr(self, "_depth"):
            return self._depth
        if self.parent is self or not self.parent:
            return 1
        else:
            self._depth = 1 + self.parent.depth
            return self._depth

    def add_child(self, other):
        if not other is self:
            self.children.add(other)

    def is_kernel_task(self):
        if "kthreadd" in self.name:
            return True
        if self.parent and not self.parent is self:
            return self.parent.is_kernel_task()
        return False

    def __hash__(self):
        return hash((self.pid, self.ppid, self.name, self.start_time))
    
    def __lt__(self, other):
        return self.depth < other.depth

    def __eq__(self, other):
        if not isinstance(other, Process):
            return False
        if not self.is_kernel_task():
            return self.start_time == other.start_time
        return self.pid == other.pid

    def __str__(self):
        return f"{self.name}_{hex(self.start_time)[2:]}".replace(":","")

# map PID to process
processes = set()
time_stop = 1000

time_start = datetime.now()

'''
There isn't a direct mapping between pid and processes. This is because pids are
recycled. We look through all processes and find the most recently started
process that matches our PID.
'''
def get_pid_object(pid):
    best = None
    for process in processes:
        if process.pid == pid:
            if not best:
                best = process
            else:
                best = process if best.start_time < process.start_time else best
    return best

nodes_to_add = {} 
nodes_to_remove = {}

'''
On asid change we iterate over the entire process list. If unseen it could be
that the process changed names or that the process is actually new. If its new
we try to resolve its parent and add it to the parent process. If its just a 
process name change we remove the old and do the same process.

Next, we cycle back through processes which failed to resolve the parent process
and rematch them with try_resolve_parent.

We also use this as a convenient place to end the recording at a certain amount
of time.
'''

@panda.cb_asid_changed
def asid_changed(env, old_asid, new_asid):
    global processes
    new_processes = set()
    pid_mapping = {}

    for process in panda.get_processes(env):
        proc_obj = Process(process)
        new_processes.add(proc_obj)
        pid_mapping[proc_obj.pid] = proc_obj
    
    processes_to_consider = list(new_processes)
    processes_to_consider.sort(key=lambda x: x.pid)
    for process in processes_to_consider:
        parent = pid_mapping[process.ppid]
        process.parent = parent
        parent.add_child(process)
    
    proc_new = set(processes_to_consider)

    new_processes = proc_new - processes
    dead_processes = processes - proc_new

    processes = proc_new

    for nodes in nodes_to_add:
        for node in new_processes:
            nodes_to_add[nodes].add(node)
    for nodes in nodes_to_remove:
        for node in dead_processes:
            nodes_to_remove[nodes].add(node)

    if datetime.now() - time_start > timedelta(seconds=time_stop):
        panda.end_analysis()
    return 0

# run some commands
@blocking
def run_cmd():
    panda.revert_sync("root")
    while True:
        print(panda.run_serial_cmd("sleep 10"))
        print(panda.run_serial_cmd("uname -a"))
        print(panda.run_serial_cmd("ls -la"))
        print(panda.run_serial_cmd("whoami"))
        print(panda.run_serial_cmd("date"))
        print(panda.run_serial_cmd("uname -a | cat | cat | cat | cat | tee /asdf"))
        print(panda.run_serial_cmd("time time time time time whoami"))
        print(panda.run_serial_cmd("sleep 10"))
        print(panda.run_serial_cmd("watch watch watch watch watch watch date &"))
	

@app.route("/")
def graph():
    g = Graph('unix', filename='process',engine='dot')
    def traverse_internal(node):
        if node is None:
            return
        for child in node.children:
            g.edge(str(node),str(child))
            traverse_internal(child)
    init = get_pid_object(0)
   
    traverse_internal(init)
    return render_template('svgtest.html', chart_output=g.source)



#random number Generator Thread
thread = Thread()
thread_stop_event = Event()

def emitEvents():
    """
    Generate a random number every 1 second and emit to a socketio instance (broadcast)
    Ideally to be run in a separate thread?
    """
    import random
    import string
    global nodes_to_add
    def get_random_string(length):
        letters = string.ascii_lowercase
        result_str = ''.join(random.choice(letters) for i in range(length))
        return result_str
    #infinite loop of magical random numbers
    my_string = get_random_string(8)
    nodes_to_add[my_string] = set()
    nodes_to_remove[my_string] = set()
    my_nodes_to_add = nodes_to_add[my_string]
    my_nodes_to_remove = nodes_to_remove[my_string]
    print(f"Making random numbers {my_string}")
    while not thread_stop_event.isSet():
        common = list(set(my_nodes_to_add).intersection(set(my_nodes_to_remove)))
        for i in common:
            my_nodes_to_add.remove(i)
            my_nodes_to_remove.remove(i)
        # sort nodes to add by depth
        nodes_to_add_sorted = list(my_nodes_to_add)
        sorted(nodes_to_add_sorted, key=lambda x: x.depth)
        if my_nodes_to_add:
            p = nodes_to_add_sorted[0]
            my_nodes_to_add.remove(p)
            print(f"emitting newprocess {p}")
            parent = p.parent
            socketio.emit('newprocess', {'operation': 'add', 'pproc': str(parent), 'child': str(p)}, namespace='/test')
        nodes_to_remove_sorted = list(my_nodes_to_remove)
        sorted(nodes_to_remove_sorted, key=lambda x: -x.depth)
        if my_nodes_to_remove:
            p = nodes_to_remove_sorted[0]
            my_nodes_to_remove.remove(p)
            print(f"emitting remove {p}")
            parent = p.parent
            socketio.emit('newprocess', {'operation': 'remove', 'pproc': str(parent), 'child': str(p)}, namespace='/test')
        socketio.sleep(0.1)

@socketio.on('connect', namespace='/test')
def test_connect():
    # need visibility of the global thread object
    global thread
    print('Client connected')

    #Start the random number generator thread only if the thread has not been started before.
    if not thread.isAlive():
        print("Starting Thread")
        thread = socketio.start_background_task(emitEvents)

@socketio.on('disconnect', namespace='/test')
def test_disconnect():
    print('Client disconnected')

def start_flask():
    socketio.run(app,host='0.0.0.0',port=8888, debug=False, use_reloader=False)

x = threading.Thread(target=start_flask)
x.start()

panda.queue_async(run_cmd)
panda.run()
thread_stop_event.set()
