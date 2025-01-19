# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import builtins
import collections
import json
import pathlib
import re
import subprocess
import unittest.mock

import deepdiff
import pytest
import setuptools

import tests.conftest


def test_extras_requirement_file_aligned():
    """
    See comment in top of "extras-requirements.txt" for explanation for what this test is for
    """
    setup_py_extras_requirements_specifiers = _import_extras_requirements()
    extras_requirements_file_specifiers = _load_requirements(
        pathlib.Path(tests.conftest.root_path) / "extras-requirements.txt"
    )
    setup_py_extras_requirements_specifiers_map = _parse_requirement_specifiers_list(
        setup_py_extras_requirements_specifiers
    )
    extras_requirements_file_specifiers_map = _parse_requirement_specifiers_list(
        extras_requirements_file_specifiers
    )
    # Since these packages are only present in the mlrun-kfp image, and also can't coexist with each other,
    # we exclude them from the comparison
    excluded_packages = ["mlrun_pipelines_kfp_v1_8", "mlrun_pipelines_kfp_v2"]
    for package in excluded_packages:
        if package in setup_py_extras_requirements_specifiers_map:
            setup_py_extras_requirements_specifiers_map.pop(package)
    assert (
        deepdiff.DeepDiff(
            setup_py_extras_requirements_specifiers_map,
            extras_requirements_file_specifiers_map,
            ignore_order=True,
        )
        == {}
    )


def test_requirement_specifiers_convention():
    """
    This test exists to verify we follow our convention for requirement specifiers which is:
    If the package major is 0, it is considered unstable, and minor changes may include backwards incompatible changes.
    Therefore we limit to patch changes only, the way to do it is to specify X.Y.Z with the ~= operator.
    If the package major is 1 or above, it is considered stable, backwards incompatible changes can only occur together
    with a major bump. Therefore we allow patch and minor changes, the way to do it is to specify X.Y with the ~=
    operator
    """
    requirement_specifiers_map = _generate_all_requirement_specifiers_map()
    print(requirement_specifiers_map)

    invalid_requirement_specifiers_map = collections.defaultdict(set)
    for requirement_name, requirement_specifiers in requirement_specifiers_map.items():
        for requirement_specifier in requirement_specifiers:
            # we don't care about what's coming after the ; (it will be something like "python_version < '3.7'")
            tested_requirement_specifier = requirement_specifier.split(";")[0]
            invalid_requirement = False
            if not tested_requirement_specifier.startswith("~="):
                invalid_requirement = True
            else:
                major_version = int(
                    tested_requirement_specifier[
                        len("~=") : tested_requirement_specifier.find(".")
                    ]
                )

                # either major or part of limited group of "stable" packages
                is_stable_requirement = major_version >= 1 or requirement_name in [
                    "wheel",
                ]
                # if it's stable we want to prevent only major changes, meaning version should be X.Y
                # if it's not stable we want to prevent major and minor changes, meaning version should be X.Y.Z
                wanted_number_of_dot_occurrences = 1 if is_stable_requirement else 2
                if (
                    tested_requirement_specifier.count(".")
                    != wanted_number_of_dot_occurrences
                ):
                    invalid_requirement = True
            if invalid_requirement:
                invalid_requirement_specifiers_map[requirement_name].add(
                    requirement_specifier
                )

    # filter out pinned requirements (==) and requirements with both upper and lower bounds
    # this is done on requirement with single specifier only, for simplicity
    for requirement_name, requirement_specifiers in list(
        invalid_requirement_specifiers_map.items()
    ):
        if requirement_specifiers and len(requirement_specifiers) == 1:
            requirement_specifier = list(requirement_specifiers)[0]
            bound_up = ">=" in requirement_specifier or "~=" in requirement_specifier
            bound_down = "<=" in requirement_specifier or "<" in requirement_specifier
            pinned = "==" in requirement_specifier
            if pinned or (bound_up and bound_down):
                invalid_requirement_specifiers_map.pop(requirement_name)
                continue

    ignored_invalid_map = {
        # See comment near requirement for why we're limiting to patch changes only for all of these
        "aiobotocore": {">=2.5.0,<2.16"},
        "storey": {"~=1.8.8"},
        "pydantic": {">=1.10.15", ">=1,<2"},
        "nuclio-sdk": {">=0.5"},
        "sphinx-book-theme": {"~=1.0.1"},
        "scipy": {"~=1.13.0"},
        # These 2 are used in a tests that is purposed to test requirement without specifiers
        "faker": {""},
        "python-dotenv": {""},
        # These are not semver
        "pyhive": {" @ git+https://github.com/v3io/PyHive.git@v0.6.999"},
        "v3io-generator": {
            " @ git+https://github.com/v3io/data-science.git#subdirectory=generator"
        },
        "databricks-sdk": {"~=0.20.0"},
        "docstring_parser": {"~=0.16"},
        "gitpython": {"~=3.1, >=3.1.41"},
        "jinja2": {"~=3.1, >=3.1.3"},
        "pyopenssl": {">=23"},
        "google-cloud-bigquery": {"[pandas, bqstorage]==3.14.1"},
        # due to a bug in 3.11
        "aiohttp": {"~=3.10.0"},
        "aiohttp-retry": {"~=2.8.0"},
        # due to a bug in apscheduler with python 3.9 https://github.com/agronholm/apscheduler/issues/770
        "apscheduler": {"~=3.6, !=3.10.2"},
        # used in tests
        "aioresponses": {"~=0.7"},
        "scikit-learn": {"~=1.5.1"},
        # ensure minimal version to gain vulnerability fixes
        "setuptools": {">=75.2"},
        "dask": {
            '~=2024.12.1; python_version >= "3.11"',
            '~=2023.12.1; python_version < "3.11"',
        },
        "distributed": {
            '~=2024.12.1; python_version >= "3.11"',
            '~=2023.12.1; python_version < "3.11"',
        },
        "dask-ml": {
            '~=1.4,<1.9.0; python_version < "3.11"',
            '~=2024.4.4; python_version >= "3.11"',
        },
        "v3io-frames": {'>=0.13.0; python_version >= "3.11"'},
    }

    for (
        ignored_requirement_name,
        ignored_specifiers,
    ) in ignored_invalid_map.items():
        if ignored_requirement_name in invalid_requirement_specifiers_map:
            diff = deepdiff.DeepDiff(
                invalid_requirement_specifiers_map[ignored_requirement_name],
                ignored_specifiers,
                ignore_order=True,
            )
            if diff == {}:
                del invalid_requirement_specifiers_map[ignored_requirement_name]

    assert invalid_requirement_specifiers_map == {}


def test_requirement_specifiers_inconsistencies():
    """
    This test exists to verify we don't have inconsistencies in requirement specifiers between different requirements
    files
    """
    requirement_specifiers_map = _generate_all_requirement_specifiers_map()
    inconsistent_specifiers_map = {}
    print(requirement_specifiers_map)
    for requirement_name, requirement_specifiers in requirement_specifiers_map.items():
        if not len(requirement_specifiers) == 1:
            inconsistent_specifiers_map[requirement_name] = requirement_specifiers

    ignored_inconsistencies_map = {
        # mlrun api must have v1 due to fastapi https://github.com/fastapi/fastapi/issues/10360
        # and the fact out pydantic currently requires v1
        # on the other hand, mlrun client can have both and thus the inconsistency
        "pydantic": {">=1,<2", ">=1.10.15"},
        # packages that require specific versions per python version
        "v3io-frames": {
            '>=0.13.0; python_version >= "3.11"',
            '~=0.10.14; python_version < "3.11"',
        },
        "dask-ml": {
            '~=2024.4.4; python_version >= "3.11"',
            '~=1.4,<1.9.0; python_version < "3.11"',
        },
        "dask": {
            '~=2024.12.1; python_version >= "3.11"',
            '~=2023.12.1; python_version < "3.11"',
        },
        "distributed": {
            '~=2024.12.1; python_version >= "3.11"',
            '~=2023.12.1; python_version < "3.11"',
        },
    }

    all_keys_verified = set(ignored_inconsistencies_map.keys())
    for (
        inconsistent_requirement_name,
        inconsistent_specifiers,
    ) in ignored_inconsistencies_map.items():
        if inconsistent_requirement_name in inconsistent_specifiers_map:
            diff = deepdiff.DeepDiff(
                inconsistent_specifiers_map[inconsistent_requirement_name],
                inconsistent_specifiers,
                ignore_order=True,
            )
            if diff == {}:
                del inconsistent_specifiers_map[inconsistent_requirement_name]
            all_keys_verified.remove(inconsistent_requirement_name)

    assert inconsistent_specifiers_map == {}
    assert (
        len(all_keys_verified) == 0
    ), f"Keys not verified: {all_keys_verified}, remove them from dictionary"


def test_requirement_from_remote():
    """
    This test checks the functionality of parsing requirement specifiers from remotes
    """
    requirement_specifiers_map = _parse_requirement_specifiers_list(
        [
            "some-package~=1.9, <1.17.50",
            "other-package==0.1",
            "git+https://github.com/mlrun/something.git@some-branch#egg=more-package",
        ]
    )
    assert len(requirement_specifiers_map) > 0
    assert requirement_specifiers_map["some-package"] == {
        "~=1.9, <1.17.50",
    }
    assert requirement_specifiers_map["other-package"] == {
        "==0.1",
    }
    assert requirement_specifiers_map["more-package"] == {
        "git+https://github.com/mlrun/something.git@some-branch",
    }


def _generate_all_requirement_specifiers_map() -> dict[str, set]:
    requirements_file_paths = list(
        pathlib.Path(tests.conftest.root_path).rglob("**/*requirements.txt")
    )
    venv_path = pathlib.Path(tests.conftest.root_path) / "venv"
    requirements_file_paths = [
        path
        for path in requirements_file_paths
        if str(venv_path) not in str(path) and path.name != "locked-requirements.txt"
    ]

    requirement_specifiers = []
    for requirements_file_path in requirements_file_paths:
        requirement_specifiers.extend(_load_requirements(requirements_file_path))

    requirement_specifiers.extend(_import_extras_requirements())

    return _parse_requirement_specifiers_list(requirement_specifiers)


def _parse_requirement_specifiers_list(
    requirement_specifiers,
) -> dict[str, set]:
    specific_module_regex = (
        r"^"
        r"(?P<requirementName>[a-zA-Z\-0-9_]+)"
        r"(?P<requirementExtra>\[[a-zA-Z\-0-9_]+\])?"
        r"(?P<requirementSpecifier>.*)"
    )
    remote_location_regex = (
        r"^(?P<requirementSpecifier>.*)#egg=(?P<requirementName>[^#]+)"
    )
    requirement_specifiers_map = collections.defaultdict(set)
    for requirement_specifier in requirement_specifiers:
        regex = (
            remote_location_regex
            if "#egg=" in requirement_specifier
            else specific_module_regex
        )
        match = re.fullmatch(regex, requirement_specifier)
        assert (
            match is not None
        ), f"Requirement specifier did not matched regex. {requirement_specifier}"
        requirement_name = match.groupdict()["requirementName"].lower()
        requirement_specifier = match.groupdict()["requirementSpecifier"]
        requirement_specifiers_map[requirement_name].add(requirement_specifier)
    return requirement_specifiers_map


def _import_extras_requirements():
    def mock_file_open(file, *args, **kwargs):
        if "setup.py" not in str(file):
            return unittest.mock.mock_open(
                read_data=json.dumps({"version": "some-ver"})
            ).return_value
        else:
            return original_open(file, *args, **kwargs)

    original_setup = setuptools.setup
    original_open = builtins.open
    setuptools.setup = lambda *args, **kwargs: 0
    builtins.open = mock_file_open

    import dependencies

    setuptools.setup = original_setup
    builtins.open = original_open

    ignored_extras = ["api", "complete", "complete-api", "all"]

    extras_requirements = []
    for extra_name, extra_requirements in dependencies.extra_requirements().items():
        if extra_name not in ignored_extras:
            extras_requirements.extend(extra_requirements)

    return extras_requirements


def _load_requirements(path):
    """
    Load dependencies from requirements file, exactly like `setup.py`
    """
    with open(path) as fp:
        deps = []
        for line in fp:
            if _is_ignored_requirement_line(line):
                continue
            line = line.strip()

            if len(line.split(" #")) > 1:
                line = line.split(" #")[0]

            # e.g.: git+https://github.com/nuclio/nuclio-jupyter.git@some-branch#egg=nuclio-jupyter
            if "#egg=" in line:
                _, package = line.split("#egg=")
                deps.append(f"{package} @ {line}")
                continue

            if line.startswith("-r"):
                path = line.split("-r", 1)[-1].strip()
                other_deps = _load_requirements(
                    pathlib.Path(__file__).resolve().parent / path
                )
                deps.extend(other_deps)
                continue

            # append package
            deps.append(line)
        return deps


def _is_ignored_requirement_line(line):
    line = line.strip()
    return (not line) or (line[0] == "#")


@pytest.mark.skipif(
    subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True)
    .stdout.decode()
    .strip()
    != "true",
    reason="Not inside a Git repository",  # e.g. in a Docker image, as happens in the CI
)
def test_scikit_learn_requirements_are_aligned() -> None:
    """
    We mention `pip install scikit-learn~=x.y.z` many times in the tutorials and
    in the Docker `requirements.txt` files, check it by running:
    git grep -n "scikit-learn.="

    This test makes sure all these versions are aligned by catching deviating version specifications.
    """
    scikit_learn_version = "1.5.1"

    escaped_version = re.escape(scikit_learn_version)
    pattern = (
        f"scikit-learn.=(?!{escaped_version})[0-9\\.]*"  # match only other versions
    )

    ignored_files = [
        "tests/test_requirements.py",  # this test file
        "docs/change-log/index.md",  # a historic document
        "docs/genai/development/working-with-rag.ipynb",  # includes a generated requirement
        "dockerfiles/mlrun-api/locked-requirements.txt",  # lock file
        "dockerfiles/mlrun/locked-requirements.txt",  # lock file
        "dockerfiles/base/locked-requirements.txt",  # lock file
        "dockerfiles/jupyter/locked-requirements.txt",  # lock file
        "dockerfiles/gpu/locked-requirements.txt",  # lock file
        "dockerfiles/test/locked-requirements.txt",  # lock file
        "dockerfiles/test-system/locked-requirements.txt",  # lock file
        "dockerfiles/mlrun-kfp/locked-requirements.txt",  # lock file
    ]
    pathspec = [f":!{file}" for file in ignored_files]

    output = subprocess.run(
        ["git", "grep", "--line-number", "--perl-regexp", pattern, "--", *pathspec],
        cwd=tests.conftest.root_path,
        capture_output=True,
    )
    no_matches = output.returncode == 1
    assert no_matches, (
        "The following files include a scikit-learn requirement which is not aligned "
        f"to version {scikit_learn_version}:\n{output.stdout.decode()}\n"
        f"returncode: {output.returncode}\n"
        f"stderr:\n{output.stderr.decode()}"
    )
