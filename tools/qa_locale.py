"""Opt-in pytest emulation of legacy Windows text defaults, never app policy.

Keep Unicode filesystem support and all explicitly declared encodings intact.
This tests file/pipe boundaries on Linux without pretending to run Windows Qt.
"""
import codecs
import io
import os
import subprocess
from contextlib import contextmanager


@contextmanager
def default_text_encoding(name):
    encoding = codecs.lookup(name).name
    original_io = io.text_encoding
    original_subprocess = subprocess._text_encoding
    def choose(value=None, stacklevel=2):
        return encoding if value is None or value == 'locale' else value
    io.text_encoding = choose
    subprocess._text_encoding = lambda: encoding
    try:
        yield encoding
    finally:
        io.text_encoding = original_io
        subprocess._text_encoding = original_subprocess


def pytest_configure(config):
    name = os.environ.get('BCC_QA_TEXT_ENCODING')
    if name:
        context = default_text_encoding(name)
        config._bcc_text_locale_context = context
        config._bcc_text_locale_name = context.__enter__()


def pytest_report_header(config):
    name = getattr(config, '_bcc_text_locale_name', None)
    if name:
        return 'Emulated default text encoding: ' + name + ' (not native Windows evidence)'


def pytest_unconfigure(config):
    context = getattr(config, '_bcc_text_locale_context', None)
    if context:
        context.__exit__(None, None, None)
