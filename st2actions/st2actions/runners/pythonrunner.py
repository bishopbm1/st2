import os
import abc
import json
import six
import sys
import traceback
import uuid
import json
import inspect
import argparse
import logging as stdlib_logging

from eventlet import greenio

from multiprocessing import Process
from st2actions.runners import ActionRunner
from st2common import log as logging
from st2common.models.api.action import ACTIONEXEC_STATUS_SUCCEEDED, ACTIONEXEC_STATUS_FAILED
from st2common.util import loader as action_loader


LOG = logging.getLogger(__name__)


def get_runner():
    return PythonRunner(str(uuid.uuid4()))


@six.add_metaclass(abc.ABCMeta)
class Action(object):
    """
    """

    def __init__(self):
        self.logger = self._set_up_logger()

    @abc.abstractmethod
    def run(self, **kwargs):
        """
        """
        pass

    def get_cli_arguments(self):
        """
        Retrieve parsed command line arguments as a dictionary.

        :rtype: ``dict``
        """
        parser = self.get_argument_parser()
        args = vars(parser.parse_args())
        return args

    def get_argument_parser(self):
        """
        Generate argument parser for this action based on the JSON definition
        file.
        """
        metadata = self._get_action_metadata()
        parameters = metadata['parameters']
        required_parameters = metadata.get('required_parameters', None)

        parser = argparse.ArgumentParser(description='')

        for parameter_name, parameter_options in parameters.items():
            name = parameter_name.replace('_', '-')
            description = parameter_options['description']
            type = parameter_options['type']
            required = parameter_name in  required_parameters

            # TODO: Support types
            parser.add_argument('--%s' % (name), help=description,
                                required=required)

        return parser

    def _get_action_metadata(self):
        """
        Retrieve metadata for this action.

        :rtype: ``dict``
        """
        file_path = inspect.getfile(self.__class__)
        file_name = os.path.basename(file_path)
        dir_name = os.path.dirname(file_path)

        metadata_file_name = file_name.replace('.py', '.json')
        metadata_file_path = os.path.join(dir_name, metadata_file_name)

        with open(metadata_file_path, 'r') as fp:
            content = fp.read()

        metadata = json.loads(content)
        return metadata

    def _set_up_logger(self):
        """
        Set up a logger which logs all the messages with level DEBUG
        and above to stderr.
        """
        logger_name = 'actions.python.%s' % (self.__class__.__name__)
        logger = logging.getLogger(logger_name)

        console = stdlib_logging.StreamHandler()
        console.setLevel(stdlib_logging.DEBUG)

        formatter = stdlib_logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
        console.setFormatter(formatter)
        logger.addHandler(console)
        logger.setLevel(stdlib_logging.DEBUG)

        return logger


class ActionWrapper(object):
    def __init__(self, entry_point, action_parameters):
        self.entry_point = entry_point
        self.action_parameters = action_parameters

    def run(self, conn):
        try:
            action = self._load_action()
            output = action.run(**self.action_parameters)
            conn.write(str(output) + '\n')
            conn.flush()
        except Exception, e:
            _, e, tb = sys.exc_info()
            data = {'error': str(e), 'traceback': ''.join(traceback.format_tb(tb, 20))}
            data = json.dumps(data)
            conn.write(data + '\n')
            conn.flush()
            sys.exit(1)
        finally:
            conn.close()

    def _load_action(self):
        actions_kls = action_loader.register_plugin(Action, self.entry_point)
        action_kls = actions_kls[0] if actions_kls and len(actions_kls) > 0 else None
        if not action_kls:
            raise Exception('%s has no action.' % self.entry_point)
        return action_kls()


class PythonRunner(ActionRunner):

    def __init__(self, _id):
        super(PythonRunner, self).__init__()
        self._id = _id

    def pre_run(self):
        pass

    def run(self, action_parameters):
        action_wrapper = ActionWrapper(self.entry_point, action_parameters)

        # We manually create a non-duplex pipe since multiprocessing.Pipe
        # doesn't play along nicely and work with eventlet
        rfd, wfd = os.pipe()

        parent_conn = greenio.GreenPipe(rfd, 'r')
        child_conn = greenio.GreenPipe(wfd, 'w', 0)

        p = Process(target=action_wrapper.run, args=(child_conn,))
        p.daemon = True

        try:
            p.start()
            p.join()
            output = parent_conn.readline()

            try:
                output = json.loads(output)
            except Exception:
                pass

            exit_code = p.exitcode
        except:
            LOG.exception('Failed to run action.')
            _, e, tb = sys.exc_info()
            exit_code = 1
            output = {'error': str(e), 'traceback': ''.join(traceback.format_tb(tb, 20))}
        finally:
            parent_conn.close()

        status = ACTIONEXEC_STATUS_SUCCEEDED if exit_code == 0 else ACTIONEXEC_STATUS_FAILED
        self.container_service.report_result(output)
        self.container_service.report_status(status)
        LOG.info('Action output : %s. exit_code : %s. status : %s', str(output), exit_code, status)
        return output is not None
