import re
import time
import logging
import socket
import sys


class ExpectFailed(AssertionError):
    '''
    Exception to represent expectation error
    '''
    pass


class Expect():
    '''
    Class to do expect-ike stuff over paramiko connection
    '''
    @staticmethod
    def expect_list(connection, regexp_list, timeout=10):
        '''
        Expect a list of expressions

        @param connection: paramiko connection
        @param regexp_list: list of tuples (regexp, return value)
        @param timeout: timeout (default to 10)
        '''
        result = ""
        count = 0
        while count < timeout:
            try:
                recv_part = connection.channel.recv(16384)
                logging.debug("RCV: " + recv_part)
                if connection.output_shell:
                    sys.stdout.write(recv_part)
                result += recv_part
            except socket.timeout:
                # socket.timeout here means 'no more data'
                pass

            for (regexp, retvalue) in regexp_list:
                # search for the first matching regexp and return desired value
                if re.match(regexp, result):
                    return retvalue
            time.sleep(1)
            count += 1
        raise ExpectFailed(result)

    @staticmethod
    def expect(connection, strexp, timeout=10):
        '''
        Expect one expression

        @param connection: paramiko connection
        @param strexp: string to convert to expression (.*string.*)
        @param timeout: timeout (default to 10)
        '''
        return Expect.expect_list(connection, [(re.compile(".*" + strexp + ".*", re.DOTALL), True)], timeout)

    @staticmethod
    def match(connection, regexp, grouplist=[1], timeout=10):
        '''
        Match against an expression

        @param connection: paramiko connection
        @param regexp: compiled regular expression
        @param grouplist: list of groups to return (defaults to [1])
        @param timeout: timeout (default to 10)

        '''
        logging.debug("MATCHING: " + regexp.pattern)
        result = ""
        count = 0
        while count < timeout:
            try:
                recv_part = connection.channel.recv(16384)
                logging.debug("RCV: " + recv_part)
                if connection.output_shell:
                    sys.stdout.write(recv_part)
                result += recv_part
            except socket.timeout:
                # socket.timeout here means 'no more data'
                pass

            match = regexp.match(result)
            if match:
                ret_list = []
                for group in grouplist:
                    logging.debug("matched: " + match.group(group))
                    ret_list.append(match.group(group))
                return ret_list
            time.sleep(1)
            count += 1
        raise ExpectFailed(result)

    @staticmethod
    def enter(connection, command):
        '''
        Enter a command to the channel (with '\n' appended)
        '''
        return connection.channel.send(command + "\n")

    @staticmethod
    def ping_pong(connection, command, strexp, timeout=10):
        '''
        Enter a command and wait for something to happen (enter + expect combined)
        '''
        Expect.enter(connection, command)
        Expect.expect(connection, strexp, timeout)

    @staticmethod
    def expect_retval(connection, command, expected_status=0, timeout=10):
        '''
        Run command and expect specified return valud
        '''
        retval = connection.recv_exit_status(command, timeout)
        if retval is None:
            raise ExpectFailed("Got timeout (%i seconds) while executing '%s'" % (timeout, command))
        elif retval != expected_status:
            raise ExpectFailed("Got %s exit status (%s expected)" % (retval, expected_status))
        if connection.output_shell:
            sys.stdout.write("Run '%s', got %i return value\n" % (command, retval))
