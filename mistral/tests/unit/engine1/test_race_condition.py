# Copyright 2014 - Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from eventlet import corolocal
from eventlet import semaphore
from oslo.config import cfg

from mistral.actions import base as action_base
from mistral.db.v2 import api as db_api
from mistral.openstack.common import log as logging
from mistral.services import action_manager as a_m
from mistral.services import workflows as wf_service
from mistral.tests.unit.engine1 import base
from mistral.workflow import states

LOG = logging.getLogger(__name__)

# Use the set_default method to set value otherwise in certain test cases
# the change in value is not permanent.
cfg.CONF.set_default('auth_enable', False, group='pecan')


WF_LONG_ACTION = """
---
version: '2.0'

wf:
  type: direct

  description: |
    The idea is to use action that runs longer than engine.start_workflow()
    method. And we need to check that engine handles this situation.

  output:
    result: <% $.result %>

  tasks:
    task1:
      action: std.block
      publish:
        result: <% $.task1 %>
"""

WF_SHORT_ACTION = """
---
version: '2.0'

wf:
  type: direct

  description: |
    The idea is to use action that runs faster than engine.start_workflow().
    And we need to check that engine handles this situation as well. This was
    a situation previously that led to a race condition in engine, method
    on_task_result() was called while DB transaction in start_workflow() was
    still active (not committed yet).
    To emulate a short action we use a workflow with two start tasks so they
    run both in parallel on the first engine iteration when we call method
    start_workflow(). First task has a short action that just returns a
    predefined result and the second task blocks until the test explicitly
    unblocks it. So the first action will always end before start_workflow()
    methods ends.

  output:
    result: <% $.result %>

  tasks:
    task1:
      action: std.echo output=1
      publish:
        result1: <% $.task1 %>

    task2:
      action: std.block
"""

ACTION_SEMAPHORE = None
TEST_SEMAPHORE = None


class BlockingAction(action_base.Action):
    def __init__(self):
        pass

    @staticmethod
    def unblock_test():
        TEST_SEMAPHORE.release()

    @staticmethod
    def wait_for_test():
        ACTION_SEMAPHORE.acquire()

    def run(self):
        self.unblock_test()
        self.wait_for_test()

        print('Action completed [eventlet_id=%s]' % corolocal.get_ident())

        return 'test'

    def test(self):
        pass


class LongActionTest(base.EngineTestCase):
    def setUp(self):
        super(LongActionTest, self).setUp()

        global ACTION_SEMAPHORE
        global TEST_SEMAPHORE

        ACTION_SEMAPHORE = semaphore.Semaphore(1)
        TEST_SEMAPHORE = semaphore.Semaphore(0)

        a_m.register_action_class(
            'std.block',
            '%s.%s' % (BlockingAction.__module__, BlockingAction.__name__),
            None
        )

    @staticmethod
    def block_action():
        ACTION_SEMAPHORE.acquire()

    @staticmethod
    def unblock_action():
        ACTION_SEMAPHORE.release()

    @staticmethod
    def wait_for_action():
        TEST_SEMAPHORE.acquire()

    def test_long_action(self):
        wf_service.create_workflows(WF_LONG_ACTION)

        self.block_action()

        wf_ex = self.engine.start_workflow('wf', None)

        wf_ex = db_api.get_workflow_execution(wf_ex.id)

        self.assertEqual(states.RUNNING, wf_ex.state)
        self.assertEqual(states.RUNNING, wf_ex.task_executions[0].state)

        self.wait_for_action()

        # Here's the point when the action is blocked but already running.
        # Do the same check again, it should always pass.
        wf_ex = db_api.get_workflow_execution(wf_ex.id)

        self.assertEqual(states.RUNNING, wf_ex.state)
        self.assertEqual(states.RUNNING, wf_ex.task_executions[0].state)

        self.unblock_action()

        self._await(lambda: self.is_execution_success(wf_ex.id))

        wf_ex = db_api.get_workflow_execution(wf_ex.id)

        self.assertDictEqual({'result': 'test'}, wf_ex.output)

    # TODO(rakhmerov): Should periodically fail now. Fix race condition.
    def test_short_action(self):
        wf_service.create_workflows(WF_SHORT_ACTION)

        self.block_action()

        wf_ex = self.engine.start_workflow('wf', None)

        wf_ex = db_api.get_workflow_execution(wf_ex.id)

        self.assertEqual(states.RUNNING, wf_ex.state)

        task_execs = wf_ex.task_executions

        task1_ex = self._assert_single_item(task_execs, name='task1')
        task2_ex = self._assert_single_item(
            task_execs,
            name='task2',
            state=states.RUNNING
        )

        self._await(lambda: self.is_task_success(task1_ex.id))

        self.unblock_action()

        self._await(lambda: self.is_task_success(task2_ex.id))
        self._await(lambda: self.is_execution_success(wf_ex.id))

        task1_ex = db_api.get_task_execution(task1_ex.id)
        task1_action_ex = db_api.get_action_executions(
            task_execution_id=task1_ex.id
        )[0]

        self.assertEqual(1, task1_action_ex.output['result'])
