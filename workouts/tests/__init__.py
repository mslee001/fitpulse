# Test-only shim: Django 4.2's BaseContext.__copy__ does copy(super()), which
# breaks on Python 3.14 — the test client copies every rendered template's
# context, so any page GET through self.client crashed with "'super' object has
# no attribute 'dicts'". Backport Django 5's implementation for the test run.
from copy import copy as _copy

from django.template import context as _context


def _base_context_copy(self):
    duplicate = _context.BaseContext()
    duplicate.__class__ = self.__class__
    duplicate.__dict__ = _copy(self.__dict__)
    duplicate.dicts = self.dicts[:]
    return duplicate


_context.BaseContext.__copy__ = _base_context_copy
