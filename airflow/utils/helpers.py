#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import re
import signal
import warnings
from datetime import datetime
from functools import reduce
from itertools import filterfalse, tee
from typing import TYPE_CHECKING, Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple, TypeVar
from urllib import parse

from flask import url_for
from jinja2 import Template

from airflow.configuration import conf
from airflow.exceptions import AirflowException
from airflow.utils.module_loading import import_string

if TYPE_CHECKING:
    from airflow.models import TaskInstance

KEY_REGEX = re.compile(r'^[\w.-]+$')
GROUP_KEY_REGEX = re.compile(r'^[\w-]+$')
CAMELCASE_TO_SNAKE_CASE_REGEX = re.compile(r'(?!^)([A-Z]+)')

T = TypeVar('T')
S = TypeVar('S')


def validate_key(k: str, max_length: int = 250):
    """Validates value used as a key."""
    if not isinstance(k, str):
        raise TypeError(f"The key has to be a string and is {type(k)}:{k}")
    if len(k) > max_length:
        raise AirflowException(f"The key has to be less than {max_length} characters")
    if not KEY_REGEX.match(k):
        raise AirflowException(
            f"The key ({k}) has to be made of alphanumeric characters, dashes, "
            f"dots and underscores exclusively"
        )


def validate_group_key(k: str, max_length: int = 200):
    """Validates value used as a group key."""
    if not isinstance(k, str):
        raise TypeError(f"The key has to be a string and is {type(k)}:{k}")
    if len(k) > max_length:
        raise AirflowException(f"The key has to be less than {max_length} characters")
    if not GROUP_KEY_REGEX.match(k):
        raise AirflowException(
            f"The key ({k!r}) has to be made of alphanumeric characters, dashes and underscores exclusively"
        )


def alchemy_to_dict(obj: Any) -> Optional[Dict]:
    """Transforms a SQLAlchemy model instance into a dictionary"""
    if not obj:
        return None
    output = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        output[col.name] = value
    return output


def ask_yesno(question: str, default: Optional[bool] = None) -> bool:
    """Helper to get a yes or no answer from the user."""
    yes = {'yes', 'y'}
    no = {'no', 'n'}

    print(question)
    while True:
        choice = input().lower()
        if choice == "" and default is not None:
            return default
        if choice in yes:
            return True
        if choice in no:
            return False
        print("Please respond with y/yes or n/no.")


def prompt_with_timeout(question: str, timeout: int, default: Optional[bool] = None) -> bool:
    """Ask the user a question and timeout if they don't respond"""

    def handler(signum, frame):
        raise AirflowException(f"Timeout {timeout}s reached")

    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        return ask_yesno(question, default)
    finally:
        signal.alarm(0)


def is_container(obj: Any) -> bool:
    """Test if an object is a container (iterable) but not a string"""
    return hasattr(obj, '__iter__') and not isinstance(obj, str)


def as_tuple(obj: Any) -> tuple:
    """
    If obj is a container, returns obj as a tuple.
    Otherwise, returns a tuple containing obj.
    """
    if is_container(obj):
        return tuple(obj)
    else:
        return tuple([obj])


def chunks(items: List[T], chunk_size: int) -> Generator[List[T], None, None]:
    """Yield successive chunks of a given size from a list of items"""
    if chunk_size <= 0:
        raise ValueError('Chunk size must be a positive integer')
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def reduce_in_chunks(fn: Callable[[S, List[T]], S], iterable: List[T], initializer: S, chunk_size: int = 0):
    """
    Reduce the given list of items by splitting it into chunks
    of the given size and passing each chunk through the reducer
    """
    if len(iterable) == 0:
        return initializer
    if chunk_size == 0:
        chunk_size = len(iterable)
    return reduce(fn, chunks(iterable, chunk_size), initializer)


def as_flattened_list(iterable: Iterable[Iterable[T]]) -> List[T]:
    """
    Return an iterable with one level flattened

    >>> as_flattened_list((('blue', 'red'), ('green', 'yellow', 'pink')))
    ['blue', 'red', 'green', 'yellow', 'pink']
    """
    return [e for i in iterable for e in i]


def parse_template_string(template_string):
    """Parses Jinja template string."""
    if "{{" in template_string:  # jinja mode
        return None, Template(template_string)
    else:
        return template_string, None


def render_log_filename(ti: "TaskInstance", try_number, filename_template) -> str:
    """
    Given task instance, try_number, filename_template, return the rendered log
    filename

    :param ti: task instance
    :param try_number: try_number of the task
    :param filename_template: filename template, which can be jinja template or
        python string template
    """
    filename_template, filename_jinja_template = parse_template_string(filename_template)
    if filename_jinja_template:
        jinja_context = ti.get_template_context()
        jinja_context['try_number'] = try_number
        return filename_jinja_template.render(**jinja_context)

    return filename_template.format(
        dag_id=ti.dag_id,
        task_id=ti.task_id,
        execution_date=ti.execution_date.isoformat(),
        try_number=try_number,
    )


def convert_camel_to_snake(camel_str: str) -> str:
    """Converts CamelCase to snake_case."""
    return CAMELCASE_TO_SNAKE_CASE_REGEX.sub(r'_\1', camel_str).lower()


def merge_dicts(dict1: Dict, dict2: Dict) -> Dict:
    """
    Merge two dicts recursively, returning new dict (input dict is not mutated).

    Lists are not concatenated. Items in dict2 overwrite those also found in dict1.
    """
    merged = dict1.copy()
    for k, v in dict2.items():
        if k in merged and isinstance(v, dict):
            merged[k] = merge_dicts(merged.get(k, {}), v)
        else:
            merged[k] = v
    return merged


def partition(pred: Callable[[T], bool], iterable: Iterable[T]) -> Tuple[Iterable[T], Iterable[T]]:
    """Use a predicate to partition entries into false entries and true entries"""
    iter_1, iter_2 = tee(iterable)
    return filterfalse(pred, iter_1), filter(pred, iter_2)


def chain(*args, **kwargs):
    """This function is deprecated. Please use `airflow.models.baseoperator.chain`."""
    warnings.warn(
        "This function is deprecated. Please use `airflow.models.baseoperator.chain`.",
        DeprecationWarning,
        stacklevel=2,
    )
    return import_string('airflow.models.baseoperator.chain')(*args, **kwargs)


def cross_downstream(*args, **kwargs):
    """This function is deprecated. Please use `airflow.models.baseoperator.cross_downstream`."""
    warnings.warn(
        "This function is deprecated. Please use `airflow.models.baseoperator.cross_downstream`.",
        DeprecationWarning,
        stacklevel=2,
    )
    return import_string('airflow.models.baseoperator.cross_downstream')(*args, **kwargs)


def build_airflow_url_with_query(query: Dict[str, Any]) -> str:
    """
    Build airflow url using base_url and default_view and provided query
    For example:
    'http://0.0.0.0:8000/base/graph?dag_id=my-task&root=&execution_date=2020-10-27T10%3A59%3A25.615587
    """
    view = conf.get('webserver', 'dag_default_view').lower()
    url = url_for(f"Airflow.{view}")
    return f"{url}?{parse.urlencode(query)}"
