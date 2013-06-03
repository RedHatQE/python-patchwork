"""
L{Connection}.
"""

import paramiko
import time
import subprocess
import os
import random
import string
import logging

_LOG = logging.getLogger(__name__)

class Connection():
    """
    Stateful object to represent connection to the host
    """
    def __init__(self, instance, username="root", key_filename=None,
                 timeout=10, output_shell=False, disable_rpyc=False):
        """
        Create connection object

        @param instance: host parameters we would like to establish connection
                         to (or just a hostname)
        @type instance: dict or str

        @param username: user name for creating ssh connection
        @type username: str

        @param key_filename: file name with ssh private key
        @type key_filename: str

        @param timeout: timeout for creating ssh connection
        @type timeout: int

        @param output_shell: write output from this connection to standard
                             output
        @type output_shell: bool
        """
        if type(instance) == str:
            self.parameters = {'private_hostname': instance, 'public_hostname': instance}
        else:
            self.parameters = instance.copy()
        # hostname is set for compatibility issues only, will be deprecated
        # in future
        if 'private_hostname' in self.parameters.keys() and \
                'public_hostname' in self.parameters.keys():
            # Custom stuff
            self.hostname = self.parameters['private_hostname']
            self.private_hostname = self.parameters['private_hostname']
            self.public_hostname = self.parameters['public_hostname']
        elif 'public_dns_name' in self.parameters.keys() and \
                'private_ip_address' in self.parameters.keys():
            # Amazon EC2/VPC instance
            if self.parameters['public_dns_name'] != '':
                # EC2
                self.hostname = self.parameters['public_dns_name']
                self.private_hostname = self.parameters['public_dns_name']
                self.public_hostname = self.parameters['public_dns_name']
            else:
                # VPC
                self.hostname = self.parameters['private_ip_address']
                self.private_hostname = self.parameters['private_ip_address']
                self.public_hostname = self.parameters['private_ip_address']
        self.username = username
        self.output_shell = output_shell
        self.key_filename = key_filename
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.disable_rpyc = disable_rpyc
        if key_filename:
            look_for_keys = False
        else:
            look_for_keys = True
        self._connect()

    def reconnect(self):
        """
        Close the connection and open a new one
        """
        self._disconnect()
        self._connect()

    def disconnect(self):
        """
        Close the connection
        """
        self._disconnect()

    def _connect(self):
        self.cli.connect(hostname=self.private_hostname,
                         username=self.username,
                         key_filename=self.key_filename)
        self.channel = self.cli.invoke_shell(width=360, height=80)
        self.sftp = self.cli.open_sftp()
        self.channel.setblocking(0)
        if not self.disable_rpyc:
            self._connect_rpyc()
        else:
            self.pbm = None
            self.rpyc = None

    def _disconnect(self):
        self.cli.close()
        self._disconnect_rpyc()

    def _connect_rpyc(self):
        try:
            import rpyc
            from plumbum import remote_machine

            devnull_fd = open("/dev/null", "w")
            rpyc_dirname = os.path.dirname(rpyc.__file__)
            rnd_id = ''.join(random.choice(string.ascii_lowercase) for x in range(10))
            pid_filename = "/tmp/%s.pid" % rnd_id
            rnd_filename = "/tmp/" + rnd_id + ".tar.gz"
            subprocess.check_call(["tar", "-cz", "--exclude", "*.pyc", "--exclude", "*.pyo", "--transform", "s,%s,%s," % (rpyc_dirname[1:][:-5], rnd_id), rpyc_dirname, "-f", rnd_filename], stdout=devnull_fd, stderr=devnull_fd)
            devnull_fd.close()
            
            self.sftp.put(rnd_filename, rnd_filename)
            os.remove(rnd_filename)
            self.recv_exit_status("tar -zxvf %s -C /tmp" % rnd_filename, 10)

            SERVER_SCRIPT = r"""
import os
print os.environ
from rpyc.utils.server import ThreadedServer
from rpyc import SlaveService
import sys
t = ThreadedServer(SlaveService, hostname = 'localhost', port = 0, reuse_addr = True)
fd = open('""" + pid_filename + r"""', 'w')
fd.write(str(t.port))
fd.close()
t.start()
"""
            stdin_rpyc, stdout_rpyc, stderr_rpyc = self.exec_command("echo \"%s\" | PYTHONPATH=\"/tmp/%s\" python " % (SERVER_SCRIPT, rnd_id))
            self.recv_exit_status("while [ ! -f %s ]; do sleep 1; done" % (pid_filename), 10)
            self.sftp.get(pid_filename, pid_filename)
            pid_fd = open(pid_filename, 'r')
            port = int(pid_fd.read())
            pid_fd.close()
            os.remove(pid_filename)

            self.pbm = remote_machine.SshMachine(host=self.private_hostname, user=self.username, keyfile=self.key_filename, ssh_opts=["-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no"])
            self.rpyc = rpyc.classic.ssh_connect(self.pbm, port)

        except Exception, e:
            self.pbm = None
            self.rpyc = None
            _LOG.debug("Failed to setup rpyc: %s" % e)

    def _disconnect_rpyc(self):
        if self.rpyc is not None:
            self.rpyc.close()
        if self.pbm is not None:
            self.pbm.close()

    def exec_command(self, command, bufsize=-1):
        """
        Execute a command in the connection

        @param command: command to execute
        @type command: str

        @param bufsize: buffer size
        @type bufsize: int

        @return: the stdin, stdout, and stderr of the executing command
        @rtype: tuple(L{paramiko.ChannelFile}, L{paramiko.ChannelFile},
                      L{paramiko.ChannelFile})

        @raise SSHException: if the server fails to execute the command
        """
        return self.cli.exec_command(command, bufsize)

    def recv_exit_status(self, command, timeout):
        """
        Executo a command and get its return value

        @param command: command to execute
        @type command: str

        @param timeout: command execution timeout
        @type timeout: int

        @return: the exit code of the process or None in case of timeout
        @rtype: int or None
        """
        status = None
        stdin, stdout, stderr = self.cli.exec_command(command)
        if stdout and stderr and stdin:
            for i in range(timeout):
                if stdout.channel.exit_status_ready():
                    status = stdout.channel.recv_exit_status()
                    break
                time.sleep(1)
            stdin.close()
            stdout.close()
            stderr.close()
        return status
