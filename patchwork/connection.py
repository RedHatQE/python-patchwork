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
import socket

_LOG = logging.getLogger(__name__)

def lazyprop(fn):
    attr_name = '_lazy_' + fn.__name__
    @property
    def _lazyprop(self):
        if not hasattr(self, attr_name):
            setattr(self, attr_name, fn(self))
        return getattr(self, attr_name)
    return _lazyprop


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
        if 'username' in self.parameters:
            self.username = self.parameters['username']
        else:
            self.username = username
        self.output_shell = output_shell
        if 'key_filename' in self.parameters:
            self.key_filename = self.parameters['key_filename']
        else:
            self.key_filename = key_filename
        self.disable_rpyc = disable_rpyc
        self.timeout = timeout

        # debugging buffers
        self.last_command = ""
        self.last_stdout = ""
        self.last_stderr = ""

        if key_filename:
            look_for_keys = False
        else:
            look_for_keys = True

        logging.getLogger("paramiko").setLevel(logging.WARNING)

    @lazyprop
    def cli(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(hostname=self.private_hostname,
                       username=self.username,
                       key_filename=self.key_filename,
                       timeout=self.timeout)
        # set keepalive
        transport = client.get_transport()
        transport.set_keepalive(3)
        return client

    @lazyprop
    def channel(self):
        # start shell, non-blocking channel
        chan = self.cli.invoke_shell(width=360, height=80)
        chan.setblocking(0)
        return chan

    @lazyprop
    def sftp(self):
        # open sftp
        return self.cli.open_sftp()

    @lazyprop
    def pbm(self):
        """ Plumbum lazy property """
        if not self.disable_rpyc:
            from plumbum import remote_machine
            logging.getLogger("plumbum").setLevel(logging.WARNING)
            return remote_machine.SshMachine(host=self.private_hostname, user=self.username, keyfile=self.key_filename, ssh_opts=["-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no"])
        else:
            return None

    @lazyprop
    def rpyc(self):
        """ RPyC lazy property """
        if not self.disable_rpyc:
            try:
                import rpyc

                devnull_fd = open("/dev/null", "w")
                rpyc_dirname = os.path.dirname(rpyc.__file__)
                rnd_id = ''.join(random.choice(string.ascii_lowercase) for x in range(10))
                pid_filename = "/tmp/%s.pid" % rnd_id
                pid_dest_filename = "/tmp/%s%s.pid" % (rnd_id, rnd_id)
                rnd_filename = "/tmp/" + rnd_id + ".tar.gz"
                rnd_dest_filename = "/tmp/" + rnd_id + rnd_id + ".tar.gz"
                subprocess.check_call(["tar", "-cz", "--exclude", "*.pyc", "--exclude", "*.pyo", "--transform", "s,%s,%s," % (rpyc_dirname[1:][:-5], rnd_id), rpyc_dirname, "-f", rnd_filename], stdout=devnull_fd, stderr=devnull_fd)
                devnull_fd.close()
            
                self.sftp.put(rnd_filename, rnd_dest_filename)
                os.remove(rnd_filename)
                self.recv_exit_status("tar -zxvf %s -C /tmp" % rnd_dest_filename, 10)

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
                self.stdin_rpyc, self.stdout_rpyc, self.stderr_rpyc = self.exec_command("echo \"%s\" | PYTHONPATH=\"/tmp/%s\" python " % (SERVER_SCRIPT, rnd_id), get_pty=True)
                self.recv_exit_status("while [ ! -f %s ]; do sleep 1; done" % (pid_filename), 10)
                self.sftp.get(pid_filename, pid_dest_filename)
                pid_fd = open(pid_dest_filename, 'r')
                port = int(pid_fd.read())
                pid_fd.close()
                os.remove(pid_dest_filename)

                return rpyc.classic.ssh_connect(self.pbm, port)

            except Exception, e:
                self.stdin_rpyc, self.stdout_rpyc, self.stderr_rpyc = None, None, None
                _LOG.debug("Failed to setup rpyc: %s" % e)
                return None
        else:
            return None

    def reconnect(self):
        """
        Close the connection and open a new one
        """
        self.disconnect()

    def disconnect(self):
        """
        Close the connection
        """
        if hasattr(self, '_lazy_cli'):
            self.cli.close()
            delattr(self, '_lazy_cli')
        if hasattr(self, '_lazy_pbm'):
            self.pbm.close()
            delattr(self, '_lazy_pbm')
        if hasattr(self, '_lazy_rpyc'):
            self.rpyc.close()
            delattr(self, '_lazy_rpyc')

    def exec_command(self, command, bufsize=-1, get_pty=False):
        """
        Execute a command in the connection

        @param command: command to execute
        @type command: str

        @param bufsize: buffer size
        @type bufsize: int

        @param get_pty: get pty
        @type get_pty: bool

        @return: the stdin, stdout, and stderr of the executing command
        @rtype: tuple(L{paramiko.ChannelFile}, L{paramiko.ChannelFile},
                      L{paramiko.ChannelFile})

        @raise SSHException: if the server fails to execute the command
        """
        self.last_command = command
        return self.cli.exec_command(command, bufsize, get_pty=get_pty)

    def recv_exit_status(self, command, timeout=10, get_pty=False):
        """
        Execute a command and get its return value

        @param command: command to execute
        @type command: str

        @param timeout: command execution timeout
        @type timeout: int

        @param get_pty: get pty
        @type get_pty: bool

        @return: the exit code of the process or None in case of timeout
        @rtype: int or None
        """
        status = None
        self.last_command = command
        stdin, stdout, stderr = self.cli.exec_command(command, get_pty=get_pty)


        if stdout and stderr and stdin:

            # prepare for async stdout handling
            stdout.channel.setblocking(False)
            self.last_stdout = ''

            timeout = time.time() + timeout

            while time.time() <= timeout:
                # handle possible channel events until either the recv_exit
                # or the timeout happens

                try:
                    self.last_stdout += stdout.read(1024)
                except socket.timeout:
                    # no events, sleep
                    time.sleep(0.1)
                    continue

                if stdout.channel.exit_status_ready():
                    status = stdout.channel.recv_exit_status()
                    break

            stdout.channel.setblocking(True)

            # drain the stderr channel
            self.last_stderr = stderr.read()

            stdin.close()
            stdout.close()
            stderr.close()

        return status
