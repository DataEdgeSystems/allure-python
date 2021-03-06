import pytest
import allure_commons
from allure_commons.utils import now
from allure_commons.utils import md5
from allure_commons.utils import uuid4
from allure_commons.utils import represent
from allure_commons.utils import platform_label
from allure_commons.utils import host_tag, thread_tag
from allure_commons.reporter import AllureReporter
from allure_commons.model2 import TestStepResult, TestResult, TestBeforeResult, TestAfterResult
from allure_commons.model2 import TestResultContainer
from allure_commons.model2 import StatusDetails
from allure_commons.model2 import Parameter
from allure_commons.model2 import Label, Link
from allure_commons.model2 import Status
from allure_commons.types import LabelType
from allure_pytest.utils import allure_description, allure_description_html
from allure_pytest.utils import allure_labels, allure_links, pytest_markers
from allure_pytest.utils import allure_full_name, allure_package, allure_name
from allure_pytest.utils import get_status, get_status_details
from allure_pytest.utils import get_outcome_status, get_outcome_status_details


class AllureListener(object):

    def __init__(self):
        self.allure_logger = AllureReporter()
        self._cache = ItemCache()
        self._host = host_tag()
        self._thread = thread_tag()

    @allure_commons.hookimpl
    def start_step(self, uuid, title, params):
        parameters = [Parameter(name=name, value=value) for name, value in params]
        step = TestStepResult(name=title, start=now(), parameters=parameters)
        self.allure_logger.start_step(None, uuid, step)

    @allure_commons.hookimpl
    def stop_step(self, uuid, exc_type, exc_val, exc_tb):
        self.allure_logger.stop_step(uuid,
                                     stop=now(),
                                     status=get_status(exc_val),
                                     statusDetails=get_status_details(exc_type, exc_val, exc_tb))

    @allure_commons.hookimpl
    def start_fixture(self, parent_uuid, uuid, name):
        after_fixture = TestAfterResult(name=name, start=now())
        self.allure_logger.start_after_fixture(parent_uuid, uuid, after_fixture)

    @allure_commons.hookimpl
    def stop_fixture(self, parent_uuid, uuid, name, exc_type, exc_val, exc_tb):
        self.allure_logger.stop_after_fixture(uuid,
                                              stop=now(),
                                              status=get_status(exc_val),
                                              statusDetails=get_status_details(exc_type, exc_val, exc_tb))

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        uuid = self._cache.set(item.nodeid)
        for fixturedef in _test_fixtures(item):
            group_uuid = self._cache.get(fixturedef)
            if not group_uuid:
                group_uuid = self._cache.set(fixturedef)
                group = TestResultContainer(uuid=group_uuid)
                self.allure_logger.start_group(group_uuid, group)
            self.allure_logger.update_group(group_uuid, children=uuid)

        test_case = TestResult(name=allure_name(item), uuid=uuid)
        test_case.description = allure_description(item)
        test_case.descriptionHtml = allure_description_html(item)

        self.allure_logger.schedule_test(uuid, test_case)
        yield

        for name, value in item.callspec.params.items() if hasattr(item, 'callspec') else ():
            test_result = self.allure_logger.get_test(uuid)
            if test_result:
                test_result.parameters.append(Parameter(name, represent(value)))

        test_case.labels.extend([Label(name=name, value=value) for name, value in allure_labels(item)])
        test_case.labels.extend([Label(name=LabelType.TAG, value=value) for value in pytest_markers(item)])
        test_case.labels.append(Label(name=LabelType.HOST, value=self._host))
        test_case.labels.append(Label(name=LabelType.THREAD, value=self._thread))
        test_case.labels.append(Label(name=LabelType.FRAMEWORK, value='pytest'))
        test_case.labels.append(Label(name=LabelType.LANGUAGE, value=platform_label()))

        test_case.links += [Link(link_type, url, name) for link_type, url, name in allure_links(item)]

        test_case.fullName = allure_full_name(item)
        test_case.historyId = md5(test_case.fullName)
        test_case.labels.append(Label('package', allure_package(item)))

        uuid = self._cache.pop(item.nodeid)
        self.allure_logger.close_test(uuid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        uuid = self._cache.get(item.nodeid)
        test_result = self.allure_logger.get_test(uuid)
        if test_result:
            test_result.start = now()

        yield

        if test_result:
            test_result.stop = now()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        fixture_name = fixturedef.argname

        container_uuid = self._cache.get(fixturedef)

        if not container_uuid:
            container_uuid = self._cache.set(fixturedef)
            container = TestResultContainer(uuid=container_uuid)
            self.allure_logger.start_group(container_uuid, container)

        self.allure_logger.update_group(container_uuid, start=now())

        before_fixture_uuid = uuid4()
        before_fixture = TestBeforeResult(name=fixture_name, start=now())
        self.allure_logger.start_before_fixture(container_uuid, before_fixture_uuid, before_fixture)

        outcome = yield

        self.allure_logger.stop_before_fixture(before_fixture_uuid,
                                               stop=now(),
                                               status=get_outcome_status(outcome),
                                               statusDetails=get_outcome_status_details(outcome))

        finalizers = getattr(fixturedef, '_finalizers', [])
        for index, finalizer in enumerate(finalizers):
            name = '{fixture}::{finalizer}'.format(fixture=fixturedef.argname, finalizer=finalizer.__name__)
            finalizers[index] = allure_commons.fixture(finalizer, parent_uuid=container_uuid, name=name)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_post_finalizer(self, fixturedef):
        yield
        if hasattr(fixturedef, 'cached_result') and self._cache.get(fixturedef):
            container_uuid = self._cache.pop(fixturedef)
            self.allure_logger.stop_group(container_uuid, stop=now())

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        uuid = self._cache.set(item.nodeid)
        report = (yield).get_result()
        allure_item = self.allure_logger.get_item(uuid)
        status = allure_item.status or None
        status_details = None

        if call.excinfo and hasattr(call.excinfo.value, 'msg'):
            status_details = StatusDetails(message=call.excinfo.value.msg)
        elif hasattr(report, 'wasxfail'):
            status_details = StatusDetails(message=report.wasxfail)
        elif report.failed:
            status_details = StatusDetails(message=call.excinfo.exconly(), trace=report.longreprtext)

        if report.when == 'setup':
            if report.passed:
                status = Status.PASSED
            if report.failed:
                status = Status.BROKEN
            if report.skipped:
                status = Status.SKIPPED

        if report.when == 'call':
            if report.passed and status == Status.PASSED:
                pass
            if report.failed:
                status = Status.FAILED
            if report.skipped:
                status = Status.SKIPPED

        if report.when == 'teardown':
            if report.failed and status == Status.PASSED:
                status = Status.BROKEN

        test_result = self.allure_logger.get_test(uuid)
        if test_result:
            if status_details:
                test_result.status = status
                test_result.statusDetails = status_details
            else:
                test_result.status = status

    @allure_commons.hookimpl
    def attach_data(self, body, name, attachment_type, extension):
        self.allure_logger.attach_data(uuid4(), body, name=name, attachment_type=attachment_type, extension=extension)

    @allure_commons.hookimpl
    def attach_file(self, source, name, attachment_type, extension):
        self.allure_logger.attach_file(uuid4(), source, name=name, attachment_type=attachment_type, extension=extension)

    @allure_commons.hookimpl
    def add_title(self, test_title):
        test_result = self.allure_logger.get_test(None)
        if test_result:
            test_result.name = test_title

    @allure_commons.hookimpl
    def add_description(self, test_description):
        test_result = self.allure_logger.get_test(None)
        if test_result:
            test_result.description = test_description

    @allure_commons.hookimpl
    def add_description_html(self, test_description_html):
        test_result = self.allure_logger.get_test(None)
        if test_result:
            test_result.descriptionHtml = test_description_html

    @allure_commons.hookimpl
    def add_link(self, url, link_type, name):
        test_result = self.allure_logger.get_test(None)
        if test_result:
            test_result.links.append(Link(link_type, url, name))

    @allure_commons.hookimpl
    def add_label(self, label_type, labels):
        test_result = self.allure_logger.get_test(None)
        for label in labels if test_result else ():
            test_result.labels.append(Label(label_type, label))


class ItemCache(object):

    def __init__(self):
        self._items = dict()

    def get(self, _id):
        return self._items.get(str(_id))

    def set(self, _id):
        return self._items.setdefault(str(_id), uuid4())

    def pop(self, _id):
        return self._items.pop(str(_id))


def _test_fixtures(item):
    fixturemanager = item.session._fixturemanager
    fixturedefs = []

    if hasattr(item, "fixturenames"):
        for name in item.fixturenames:
            fixturedef = fixturemanager.getfixturedefs(name, item.nodeid)
            if fixturedef:
                fixturedefs.append(fixturedef[-1])

    return fixturedefs
