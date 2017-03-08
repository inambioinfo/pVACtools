import os
import re
import shutil
from flask import current_app
import json
import sys
from subprocess import TimeoutExpired
from .utils import descriptions

spinner = re.compile(r'[\\\b\-/|]{2,}')

def fetch_process(id,data,children):
    """Fetch a tuple of available process info"""
    return (
        # This entry should always exist.  Any spawned process will have an entry in data
        data['process-%d'%id] if 'process-%d'%id in data else {},
        # This will only exist if spawned by the current run of the server
        # If the server crashed and restarted, the child is orphaned,
        # and the server will only be able to read its output files
        # (unable to access the return code or terminate it)
        children[id] if id in children else False
    )

def is_running(process):
    """Returns True if the requested process looks like it's still running"""
    if not process[0]:
        return False  # The process doesn't exist
    if process[1]:
        return process[1].poll() == None
    try:
        # check if the process is active by sending a dummy signal
        os.kill(process[0]['pid'], 0)
    except ProcessLookupError:
        return False
    return True


def processes():
    """Returns a list of processes, and whether or not each process is running"""
    data = current_app.config['storage']['loader']()
    #Python comprehensions are great!
    return [
         {
             'id':proc[0],
             'running':is_running(proc[1]),
             'url':'/api/v1/processes/%d'%proc[0],
             'results_url':'/api/v1/processes/%d/results'%proc[0],
             'attached':bool(proc[1][1]),
             'output':os.path.relpath(
                 proc[1][0]['output'],
                 os.path.join(
                     current_app.config['files']['data-dir'],
                     'results'
                 )
             ),
             'pid':proc[1][0]['pid'],
             'command':proc[1][0]['command'],
             'files':[
                 {
                     'fileID':fileID,
                     'url':'/api/v1/processes/%d/results/%s'%(
                         proc[0],
                         fileID
                     ),
                     'display_name':filedata['display_name'],
                     'description':filedata['description']
                 }
                 for (fileID, filedata) in data['process-%d'%proc[0]]['files'].items()
             ],
             'parameters':(
                 json.load(open(os.path.join(
                     proc[1][0]['output'],
                     'config.json'
                 )))
                 if os.path.isfile(os.path.join(
                     proc[1][0]['output'],
                     'config.json'
                 )) else {}
             )
         } for proc in
            map(
                lambda x: (x,fetch_process(
                    x,
                    data,
                    current_app.config['storage']['children']
                )),
                range(data['processid']+1)
            ) if 'process-%d'%(proc[0]) in data
    ]


def process_info(id):
    """Returns more detailed information about a specific process"""
    data = current_app.config['storage']['loader']()
    process = fetch_process(id, data, current_app.config['storage']['children'])
    if not process[0]:
        return (
            {
                "code":400,
                "message":"The requested process (%d) does not exist"%id,
                "fields":"id"
            },400
        )
    reader = open(process[0]['logfile'])
    log = spinner.sub('', reader.read()).strip().split(os.linesep)
    process[0]['status'] = log[-1]
    reader.close()
    if not is_running(process):
        if process[1]:
            process[0]['status'] = "Process Complete: %d"%process[1].returncode
        else:
            process[0]['status'] = "Process Complete"
        # If there is a staging directory, remove it
        if os.path.isdir(os.path.join(process[0]['output'], 'Staging')):
            shutil.rmtree(os.path.join(process[0]['output'], 'Staging'))
    data.save()
    configfile = os.path.join(
        process[0]['output'],
        'config.json'
    )
    return {
        'pid':process[0]['pid'],#
        'id':id,#
        'results_url':'/api/v1/processes/%d/results'%id,#
        'attached': bool(process[1]),#
        'command':process[0]['command'],#
        'status':process[0]['status'],
        'log':log,
        'log_updated_at':(
            int(os.stat(process[0]['logfile']).st_mtime)
            if os.path.isfile(process[0]['logfile'])
            else 0
        ),
        'output':os.path.relpath(
            process[0]['output'],
            os.path.join(
                current_app.config['files']['data-dir'],
                'results'
            )
        ),#
        'running':is_running(process),#
        'files':[
            {
                'fileID':fileID,
                'url':'/api/v1/processes/%d/results/%s'%(
                    id,
                    fileID
                ),
                'display_name':filedata['display_name'],
                'description':filedata['description']
            }
            for (fileID, filedata) in data['process-%d'%id]['files'].items()
        ],
        'parameters':(#
            json.load(open(configfile))
            if os.path.isfile(configfile)
            else {}
        )
    }


def stop(id):
    """Stops the requested process.  This is only allowed if the child is still attached"""
    data = current_app.config['storage']['loader']()
    status = process_info(id)
    if type(status) == dict:  # status could be an error object if the id is invalid
        if status['running'] and status['pid']>1:
            current_app.config['storage']['children'][id].terminate()
    return status


def shutdown():
    """Stops all attached, running children"""
    data = current_app.config['storage']['loader']()
    output = []
    for i in range(data['processid']+1):
        proc = fetch_process(i, data, current_app.config['storage']['children'])
        if is_running(proc) and i in current_app.config['storage']['children']:
            output.append(i)
            try:
                current_app.config['storage']['children'][i].wait(.1)
            except TimeoutExpired:
                current_app.config['storage']['children'][i].terminate()
    return output


def reset(clearall):
    """Clears out finished processes from the record"""
    data = current_app.config['storage']['loader']()
    output = []
    for i in range(data['processid']+1):
        proc = fetch_process(i, data, current_app.config['storage']['children'])
        if 'process-%d'%i in data and not is_running(proc):
            if i not in current_app.config['storage']['children']:
                try:
                    shutil.rmtree(data['process-%d'%i]['output'])
                except FileNotFoundError:
                    pass
                del data['process-%d'%i]
                output.append(i)
            elif clearall:
                try:
                    shutil.rmtree(data['process-%d'%i]['output'])
                except FileNotFoundError:
                    pass
                del data['process-%d'%i]
                del current_app.config['storage']['children'][i]
                output.append(i)
    # Set the processid to the highest child process from this session
    data['processid'] = max([0]+[i for i in range(data['processid']+1) if 'process-%d'%i in data])
    if clearall and 'reboot' in data:
        del data['reboot']
    data.save()
    return output