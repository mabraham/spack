# Copyright 2013-2024 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""This module contains functions related to finding compilers on the
system and configuring Spack to use multiple compilers.
"""
import collections
import importlib
import os
import sys
import warnings
from typing import Dict, List, Optional

import archspec.cpu

import llnl.util.filesystem as fs
import llnl.util.lang
import llnl.util.tty as tty

import spack.compiler
import spack.config
import spack.error
import spack.paths
import spack.platforms
import spack.repo
import spack.spec
import spack.version
from spack.operating_systems import windows_os
from spack.util.environment import get_path
from spack.util.naming import mod_to_class

_path_instance_vars = ["cc", "cxx", "f77", "fc"]
_flags_instance_vars = ["cflags", "cppflags", "cxxflags", "fflags"]
_other_instance_vars = [
    "modules",
    "operating_system",
    "environment",
    "implicit_rpaths",
    "extra_rpaths",
]

# TODO: Caches at module level make it difficult to mock configurations in
# TODO: unit tests. It might be worth reworking their implementation.
#: cache of compilers constructed from config data, keyed by config entry id.
_compiler_cache: Dict[str, "spack.compiler.Compiler"] = {}

_compiler_to_pkg = {
    "clang": "llvm+clang",
    "oneapi": "intel-oneapi-compilers",
    "rocmcc": "llvm-amdgpu",
    "intel@2020:": "intel-oneapi-compilers-classic",
    "arm": "acfl",
}

# TODO: generating this from the previous dict causes docs errors
package_name_to_compiler_name = {
    "llvm": "clang",
    "intel-oneapi-compilers": "oneapi",
    "llvm-amdgpu": "rocmcc",
    "intel-oneapi-compilers-classic": "intel",
    "acfl": "arm",
}


#: Tag used to identify packages providing a compiler
COMPILER_TAG = "compiler"


def pkg_spec_for_compiler(cspec):
    """Return the spec of the package that provides the compiler."""
    for spec, package in _compiler_to_pkg.items():
        if cspec.satisfies(spec):
            spec_str = "%s@%s" % (package, cspec.versions)
            break
    else:
        spec_str = str(cspec)
    return spack.spec.parse_with_version_concrete(spec_str)


def _auto_compiler_spec(function):
    def converter(cspec_like, *args, **kwargs):
        if not isinstance(cspec_like, spack.spec.CompilerSpec):
            cspec_like = spack.spec.CompilerSpec(cspec_like)
        return function(cspec_like, *args, **kwargs)

    return converter


def _to_dict(compiler):
    """Return a dict version of compiler suitable to insert in YAML."""
    d = {}
    d["spec"] = str(compiler.spec)
    d["paths"] = dict((attr, getattr(compiler, attr, None)) for attr in _path_instance_vars)
    d["flags"] = dict((fname, " ".join(fvals)) for fname, fvals in compiler.flags.items())
    d["flags"].update(
        dict(
            (attr, getattr(compiler, attr, None))
            for attr in _flags_instance_vars
            if hasattr(compiler, attr)
        )
    )
    d["operating_system"] = str(compiler.operating_system)
    d["target"] = str(compiler.target)
    d["modules"] = compiler.modules or []
    d["environment"] = compiler.environment or {}
    d["extra_rpaths"] = compiler.extra_rpaths or []
    if compiler.enable_implicit_rpaths is not None:
        d["implicit_rpaths"] = compiler.enable_implicit_rpaths

    if compiler.alias:
        d["alias"] = compiler.alias

    return {"compiler": d}


def get_compiler_config(
    configuration: "spack.config.Configuration",
    *,
    scope: Optional[str] = None,
    init_config: bool = False,
) -> List[Dict]:
    """Return the compiler configuration for the specified architecture."""
    config = configuration.get("compilers", scope=scope) or []
    if config or not init_config:
        return config

    merged_config = configuration.get("compilers")
    if merged_config:
        # Config is empty for this scope
        # Do not init config because there is a non-empty scope
        return config

    find_compilers(scope=scope)
    config = configuration.get("compilers", scope=scope)
    return config


def get_compiler_config_from_packages(
    configuration: "spack.config.Configuration", *, scope: Optional[str] = None
) -> List[Dict]:
    """Return the compiler configuration from packages.yaml"""
    packages_yaml = configuration.get("packages", scope=scope)
    return CompilerConfigFactory.from_packages_yaml(packages_yaml)


def compiler_config_files():
    config_files = list()
    config = spack.config.CONFIG
    for scope in config.writable_scopes:
        name = scope.name
        compiler_config = config.get("compilers", scope=name)
        if compiler_config:
            config_files.append(config.get_config_filename(name, "compilers"))
        compiler_config_from_packages = get_compiler_config_from_packages(config, scope=name)
        if compiler_config_from_packages:
            config_files.append(config.get_config_filename(name, "packages"))
    return config_files


def add_compilers_to_config(compilers, scope=None):
    """Add compilers to the config for the specified architecture.

    Arguments:
        compilers: a list of Compiler objects.
        scope: configuration scope to modify.
    """
    compiler_config = get_compiler_config(configuration=spack.config.CONFIG, scope=scope)
    for compiler in compilers:
        if not compiler.cc:
            tty.debug(f"{compiler.spec} does not have a C compiler")
        if not compiler.cxx:
            tty.debug(f"{compiler.spec} does not have a C++ compiler")
        if not compiler.f77:
            tty.debug(f"{compiler.spec} does not have a Fortran77 compiler")
        if not compiler.fc:
            tty.debug(f"{compiler.spec} does not have a Fortran compiler")
        compiler_config.append(_to_dict(compiler))
    spack.config.set("compilers", compiler_config, scope=scope)


@_auto_compiler_spec
def remove_compiler_from_config(compiler_spec, scope=None):
    """Remove compilers from configuration by spec.

    If scope is None, all the scopes are searched for removal.

    Arguments:
        compiler_spec: compiler to be removed
        scope: configuration scope to modify
    """
    candidate_scopes = [scope]
    if scope is None:
        candidate_scopes = spack.config.CONFIG.scopes.keys()

    removal_happened = False
    for current_scope in candidate_scopes:
        removal_happened |= _remove_compiler_from_scope(compiler_spec, scope=current_scope)

    msg = "`spack compiler remove` will not remove compilers defined in packages.yaml"
    msg += "\nTo remove these compilers, either edit the config or use `spack external remove`"
    tty.debug(msg)
    return removal_happened


def _remove_compiler_from_scope(compiler_spec, scope):
    """Removes a compiler from a specific configuration scope.

    Args:
        compiler_spec: compiler to be removed
        scope: configuration scope under consideration

    Returns:
         True if one or more compiler entries were actually removed, False otherwise
    """
    assert scope is not None, "a specific scope is needed when calling this function"
    compiler_config = get_compiler_config(configuration=spack.config.CONFIG, scope=scope)
    filtered_compiler_config = [
        compiler_entry
        for compiler_entry in compiler_config
        if not spack.spec.parse_with_version_concrete(
            compiler_entry["compiler"]["spec"], compiler=True
        ).satisfies(compiler_spec)
    ]

    if len(filtered_compiler_config) == len(compiler_config):
        return False

    # We need to preserve the YAML type for comments, hence we are copying the
    # items in the list that has just been retrieved
    compiler_config[:] = filtered_compiler_config
    spack.config.CONFIG.set("compilers", compiler_config, scope=scope)
    return True


def all_compilers_config(
    configuration: "spack.config.Configuration",
    *,
    scope: Optional[str] = None,
    init_config: bool = True,
) -> List["spack.compiler.Compiler"]:
    """Return a set of specs for all the compiler versions currently
    available to build with.  These are instances of CompilerSpec.
    """
    from_packages_yaml = get_compiler_config_from_packages(configuration, scope=scope)
    if from_packages_yaml:
        init_config = False
    from_compilers_yaml = get_compiler_config(configuration, scope=scope, init_config=init_config)

    result = from_compilers_yaml + from_packages_yaml
    # Dedupe entries by the compiler they represent
    # If the entry is invalid, treat it as unique for deduplication
    key = lambda c: _compiler_from_config_entry(c["compiler"] or id(c))
    return list(llnl.util.lang.dedupe(result, key=key))


def all_compiler_specs(scope=None, init_config=True):
    # Return compiler specs from the merged config.
    return [
        spack.spec.parse_with_version_concrete(s["compiler"]["spec"], compiler=True)
        for s in all_compilers_config(spack.config.CONFIG, scope=scope, init_config=init_config)
    ]


def find_compilers(
    path_hints: Optional[List[str]] = None,
    *,
    scope: Optional[str] = None,
    mixed_toolchain: bool = False,
    max_workers: Optional[int] = None,
) -> List["spack.compiler.Compiler"]:
    """Searches for compiler in the paths given as argument. If any new compiler is found, the
    configuration is updated, and the list of new compiler objects is returned.

    Args:
        path_hints: list of path hints where to look for. A sensible default based on the ``PATH``
            environment variable will be used if the value is None
        scope: configuration scope to modify
        mixed_toolchain: allow mixing compilers from different toolchains if otherwise missing for
            a certain language
        max_workers: number of processes used to search for compilers
    """
    import spack.detection

    known_compilers = set(all_compilers(init_config=False))

    if path_hints is None:
        path_hints = get_path("PATH")
    default_paths = fs.search_paths_for_executables(*path_hints)
    if sys.platform == "win32":
        default_paths.extend(windows_os.WindowsOs().compiler_search_paths)
    compiler_pkgs = spack.repo.PATH.packages_with_tags(COMPILER_TAG, full=True)

    detected_packages = spack.detection.by_path(
        compiler_pkgs, path_hints=default_paths, max_workers=max_workers
    )

    valid_compilers = {}
    for name, detected in detected_packages.items():
        compilers = [x for x in detected if CompilerConfigFactory.from_external_spec(x.spec)]
        if not compilers:
            continue
        valid_compilers[name] = compilers

    def _has_fortran_compilers(x):
        if "compilers" not in x.spec.extra_attributes:
            return False

        return "fortran" in x.spec.extra_attributes["compilers"]

    if mixed_toolchain:
        gccs = [x for x in valid_compilers.get("gcc", []) if _has_fortran_compilers(x)]
        if gccs:
            best_gcc = sorted(
                gccs, key=lambda x: spack.spec.parse_with_version_concrete(x.spec).version
            )[-1]
            gfortran = best_gcc.spec.extra_attributes["compilers"]["fortran"]
            for name in ("llvm", "apple-clang"):
                if name not in valid_compilers:
                    continue
                candidates = valid_compilers[name]
                for candidate in candidates:
                    if _has_fortran_compilers(candidate):
                        continue
                    candidate.spec.extra_attributes["compilers"]["fortran"] = gfortran

    new_compilers = []
    for name, detected in valid_compilers.items():
        for config in CompilerConfigFactory.from_specs([x.spec for x in detected]):
            c = _compiler_from_config_entry(config["compiler"])
            if c in known_compilers:
                continue
            new_compilers.append(c)

    add_compilers_to_config(new_compilers, scope=scope)
    return new_compilers


def select_new_compilers(compilers, scope=None):
    """Given a list of compilers, remove those that are already defined in
    the configuration.
    """
    compilers_not_in_config = []
    for c in compilers:
        arch_spec = spack.spec.ArchSpec((None, c.operating_system, c.target))
        same_specs = compilers_for_spec(
            c.spec, arch_spec=arch_spec, scope=scope, init_config=False
        )
        if not same_specs:
            compilers_not_in_config.append(c)

    return compilers_not_in_config


def supported_compilers() -> List[str]:
    """Return a set of names of compilers supported by Spack.

    See available_compilers() to get a list of all the available
    versions of supported compilers.
    """
    # Hack to be able to call the compiler `apple-clang` while still
    # using a valid python name for the module
    return sorted(all_compiler_names())


def supported_compilers_for_host_platform() -> List[str]:
    """Return a set of compiler class objects supported by Spack
    that are also supported by the current host platform
    """
    host_plat = spack.platforms.real_host()
    return supported_compilers_for_platform(host_plat)


def supported_compilers_for_platform(platform: "spack.platforms.Platform") -> List[str]:
    """Return a set of compiler class objects supported by Spack
    that are also supported by the provided platform

    Args:
        platform (str): string representation of platform
            for which compiler compatability should be determined
    """
    return [
        name
        for name in supported_compilers()
        if class_for_compiler_name(name).is_supported_on_platform(platform)
    ]


def all_compiler_names() -> List[str]:
    def replace_apple_clang(name):
        return name if name != "apple_clang" else "apple-clang"

    return [replace_apple_clang(name) for name in all_compiler_module_names()]


@llnl.util.lang.memoized
def all_compiler_module_names() -> List[str]:
    return list(llnl.util.lang.list_modules(spack.paths.compilers_path))


@_auto_compiler_spec
def supported(compiler_spec):
    """Test if a particular compiler is supported."""
    return compiler_spec.name in supported_compilers()


@_auto_compiler_spec
def find(compiler_spec, scope=None, init_config=True):
    """Return specs of available compilers that match the supplied
    compiler spec.  Return an empty list if nothing found."""
    return [c for c in all_compiler_specs(scope, init_config) if c.satisfies(compiler_spec)]


@_auto_compiler_spec
def find_specs_by_arch(compiler_spec, arch_spec, scope=None, init_config=True):
    """Return specs of available compilers that match the supplied
    compiler spec.  Return an empty list if nothing found."""
    return [
        c.spec
        for c in compilers_for_spec(
            compiler_spec, arch_spec=arch_spec, scope=scope, init_config=init_config
        )
    ]


def all_compilers(scope=None, init_config=True):
    return all_compilers_from(
        configuration=spack.config.CONFIG, scope=scope, init_config=init_config
    )


def all_compilers_from(configuration, scope=None, init_config=True):
    compilers = []
    for items in all_compilers_config(
        configuration=configuration, scope=scope, init_config=init_config
    ):
        items = items["compiler"]
        compiler = _compiler_from_config_entry(items)  # can be None in error case
        if compiler:
            compilers.append(compiler)
    return compilers


@_auto_compiler_spec
def compilers_for_spec(compiler_spec, *, arch_spec=None, scope=None, init_config=True):
    """This gets all compilers that satisfy the supplied CompilerSpec.
    Returns an empty list if none are found.
    """
    config = all_compilers_config(spack.config.CONFIG, scope=scope, init_config=init_config)
    matches = set(find(compiler_spec, scope, init_config))
    compilers = []
    for cspec in matches:
        compilers.extend(get_compilers(config, cspec, arch_spec))
    return compilers


def compilers_for_arch(arch_spec, scope=None):
    config = all_compilers_config(spack.config.CONFIG, scope=scope, init_config=False)
    return list(get_compilers(config, arch_spec=arch_spec))


def compiler_specs_for_arch(arch_spec, scope=None):
    return [c.spec for c in compilers_for_arch(arch_spec, scope)]


class CacheReference:
    """This acts as a hashable reference to any object (regardless of whether
    the object itself is hashable) and also prevents the object from being
    garbage-collected (so if two CacheReference objects are equal, they
    will refer to the same object, since it will not have been gc'ed since
    the creation of the first CacheReference).
    """

    def __init__(self, val):
        self.val = val
        self.id = id(val)

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, CacheReference) and self.id == other.id


def compiler_from_dict(items):
    cspec = spack.spec.parse_with_version_concrete(items["spec"], compiler=True)
    os = items.get("operating_system", None)
    target = items.get("target", None)

    if not ("paths" in items and all(n in items["paths"] for n in _path_instance_vars)):
        raise InvalidCompilerConfigurationError(cspec)

    cls = class_for_compiler_name(cspec.name)

    compiler_paths = []
    for c in _path_instance_vars:
        compiler_path = items["paths"][c]
        if compiler_path != "None":
            compiler_paths.append(compiler_path)
        else:
            compiler_paths.append(None)

    mods = items.get("modules")
    if mods == "None":
        mods = []

    alias = items.get("alias", None)
    compiler_flags = items.get("flags", {})
    environment = items.get("environment", {})
    extra_rpaths = items.get("extra_rpaths", [])
    implicit_rpaths = items.get("implicit_rpaths", None)

    # Starting with c22a145, 'implicit_rpaths' was a list. Now it is a
    # boolean which can be set by the user to disable all automatic
    # RPATH insertion of compiler libraries
    if implicit_rpaths is not None and not isinstance(implicit_rpaths, bool):
        implicit_rpaths = None

    return cls(
        cspec,
        os,
        target,
        compiler_paths,
        mods,
        alias,
        environment,
        extra_rpaths,
        enable_implicit_rpaths=implicit_rpaths,
        **compiler_flags,
    )


def _compiler_from_config_entry(items):
    """Note this is intended for internal use only. To avoid re-parsing
    the same config dictionary this keeps track of its location in
    memory. If you provide the same dictionary twice it will return
    the same Compiler object (regardless of whether the dictionary
    entries have changed).
    """
    config_id = CacheReference(items)
    compiler = _compiler_cache.get(config_id, None)

    if compiler is None:
        try:
            compiler = compiler_from_dict(items)
        except UnknownCompilerError as e:
            warnings.warn(e.message)
        _compiler_cache[config_id] = compiler

    return compiler


def get_compilers(config, cspec=None, arch_spec=None):
    compilers = []

    for items in config:
        items = items["compiler"]

        # We might use equality here.
        if cspec and not spack.spec.parse_with_version_concrete(
            items["spec"], compiler=True
        ).satisfies(cspec):
            continue

        # If an arch spec is given, confirm that this compiler
        # is for the given operating system
        os = items.get("operating_system", None)
        if arch_spec and os != arch_spec.os:
            continue

        # If an arch spec is given, confirm that this compiler
        # is for the given target. If the target is 'any', match
        # any given arch spec. If the compiler has no assigned
        # target this is an old compiler config file, skip this logic.
        target = items.get("target", None)

        try:
            current_target = archspec.cpu.TARGETS[str(arch_spec.target)]
            family = str(current_target.family)
        except KeyError:
            # TODO: Check if this exception handling makes sense, or if we
            # TODO: need to change / refactor tests
            family = arch_spec.target
        except AttributeError:
            assert arch_spec is None

        if arch_spec and target and (target != family and target != "any"):
            # If the family of the target is the family we are seeking,
            # there's an error in the underlying configuration
            if archspec.cpu.TARGETS[target].family == family:
                msg = (
                    'the "target" field in compilers.yaml accepts only '
                    'target families [replace "{0}" with "{1}"'
                    ' in "{2}" specification]'
                )
                msg = msg.format(str(target), family, items.get("spec", "??"))
                raise ValueError(msg)
            continue

        compiler = _compiler_from_config_entry(items)
        if compiler:
            compilers.append(compiler)

    return compilers


@_auto_compiler_spec
def compiler_for_spec(compiler_spec, arch_spec):
    """Get the compiler that satisfies compiler_spec.  compiler_spec must
    be concrete."""
    assert compiler_spec.concrete
    assert arch_spec.concrete

    compilers = compilers_for_spec(compiler_spec, arch_spec=arch_spec)
    if len(compilers) < 1:
        raise NoCompilerForSpecError(compiler_spec, arch_spec.os)
    if len(compilers) > 1:
        msg = "Multiple definitions of compiler %s " % compiler_spec
        msg += "for architecture %s:\n %s" % (arch_spec, compilers)
        tty.debug(msg)
    return compilers[0]


@_auto_compiler_spec
def get_compiler_duplicates(compiler_spec, arch_spec):
    config = spack.config.CONFIG

    scope_to_compilers = {}
    for scope in config.scopes:
        compilers = compilers_for_spec(compiler_spec, arch_spec=arch_spec, scope=scope)
        if compilers:
            scope_to_compilers[scope] = compilers

    cfg_file_to_duplicates = {}
    for scope, compilers in scope_to_compilers.items():
        config_file = config.get_config_filename(scope, "compilers")
        cfg_file_to_duplicates[config_file] = compilers

    return cfg_file_to_duplicates


@llnl.util.lang.memoized
def class_for_compiler_name(compiler_name):
    """Given a compiler module name, get the corresponding Compiler class."""
    if not supported(compiler_name):
        raise UnknownCompilerError(compiler_name)

    # Hack to be able to call the compiler `apple-clang` while still
    # using a valid python name for the module
    submodule_name = compiler_name
    if compiler_name == "apple-clang":
        submodule_name = compiler_name.replace("-", "_")

    module_name = ".".join(["spack", "compilers", submodule_name])
    module_obj = importlib.import_module(module_name)
    cls = getattr(module_obj, mod_to_class(compiler_name))

    # make a note of the name in the module so we can get to it easily.
    cls.name = compiler_name

    return cls


def all_os_classes():
    """
    Return the list of classes for all operating systems available on
    this platform
    """
    classes = []

    platform = spack.platforms.host()
    for os_class in platform.operating_sys.values():
        classes.append(os_class)

    return classes


def all_compiler_types():
    return [class_for_compiler_name(c) for c in supported_compilers()]


#: Gathers the attribute values by which a detected compiler is considered
#: unique in Spack.
#:
#:  - os: the operating system
#:  - compiler_name: the name of the compiler (e.g. 'gcc', 'clang', etc.)
#:  - version: the version of the compiler
#:
CompilerID = collections.namedtuple("CompilerID", ["os", "compiler_name", "version"])

#: Variations on a matched compiler name
NameVariation = collections.namedtuple("NameVariation", ["prefix", "suffix"])

#: Groups together the arguments needed by `detect_version`. The four entries
#: in the tuple are:
#:
#: - id: An instance of the CompilerID named tuple (version can be set to None
#:       as it will be detected later)
#: - variation: a NameVariation for file being tested
#: - language: compiler language being tested (one of 'cc', 'cxx', 'fc', 'f77')
#: - path: full path to the executable being tested
#:
DetectVersionArgs = collections.namedtuple(
    "DetectVersionArgs", ["id", "variation", "language", "path"]
)


def is_mixed_toolchain(compiler):
    """Returns True if the current compiler is a mixed toolchain,
    False otherwise.

    Args:
        compiler (spack.compiler.Compiler): a valid compiler object
    """
    cc = os.path.basename(compiler.cc or "")
    cxx = os.path.basename(compiler.cxx or "")
    f77 = os.path.basename(compiler.f77 or "")
    fc = os.path.basename(compiler.fc or "")

    toolchains = set()
    for compiler_cls in all_compiler_types():
        # Inspect all the compiler toolchain we know. If a compiler is the
        # only compiler supported there it belongs to that toolchain.
        def name_matches(name, name_list):
            # This is such that 'gcc' matches variations
            # like 'ggc-9' etc that are found in distros
            name, _, _ = name.partition("-")
            return len(name_list) == 1 and name and name in name_list

        if any(
            [
                name_matches(cc, compiler_cls.cc_names),
                name_matches(cxx, compiler_cls.cxx_names),
                name_matches(f77, compiler_cls.f77_names),
                name_matches(fc, compiler_cls.fc_names),
            ]
        ):
            tty.debug("[TOOLCHAIN] MATCH {0}".format(compiler_cls.__name__))
            toolchains.add(compiler_cls.__name__)

    if len(toolchains) > 1:
        if (
            toolchains == set(["Clang", "AppleClang", "Aocc"])
            # Msvc toolchain uses Intel ifx
            or toolchains == set(["Msvc", "Dpcpp", "Oneapi"])
        ):
            return False
        tty.debug("[TOOLCHAINS] {0}".format(toolchains))
        return True

    return False


_EXTRA_ATTRIBUTES_KEY = "extra_attributes"
_COMPILERS_KEY = "compilers"
_C_KEY = "c"
_CXX_KEY, _FORTRAN_KEY = "cxx", "fortran"


class CompilerConfigFactory:
    """Class aggregating all ways of constructing a list of compiler config entries."""

    @staticmethod
    def from_specs(specs: List["spack.spec.Spec"]) -> List[dict]:
        result = []
        compiler_package_names = supported_compilers() + list(package_name_to_compiler_name.keys())
        for s in specs:
            if s.name not in compiler_package_names:
                continue

            candidate = CompilerConfigFactory.from_external_spec(s)
            if candidate is None:
                continue

            result.append(candidate)
        return result

    @staticmethod
    def from_packages_yaml(packages_yaml) -> List[dict]:
        compiler_specs = []
        compiler_package_names = supported_compilers() + list(package_name_to_compiler_name.keys())
        for name, entry in packages_yaml.items():
            if name not in compiler_package_names:
                continue

            externals_config = entry.get("externals", None)
            if not externals_config:
                continue

            current_specs = []
            for current_external in externals_config:
                compiler = CompilerConfigFactory._spec_from_external_config(current_external)
                if compiler:
                    current_specs.append(compiler)
            compiler_specs.extend(current_specs)

        return CompilerConfigFactory.from_specs(compiler_specs)

    @staticmethod
    def _spec_from_external_config(config):
        # Allow `@x.y.z` instead of `@=x.y.z`
        err_header = f"The external spec '{config['spec']}' cannot be used as a compiler"
        # If extra_attributes is not there I might not want to use this entry as a compiler,
        # therefore just leave a debug message, but don't be loud with a warning.
        if _EXTRA_ATTRIBUTES_KEY not in config:
            tty.debug(f"[{__file__}] {err_header}: missing the '{_EXTRA_ATTRIBUTES_KEY}' key")
            return None
        extra_attributes = config[_EXTRA_ATTRIBUTES_KEY]
        result = spack.spec.Spec(
            str(spack.spec.parse_with_version_concrete(config["spec"])),
            external_modules=config.get("modules"),
        )
        result.extra_attributes = extra_attributes
        return result

    @staticmethod
    def from_external_spec(spec: "spack.spec.Spec") -> Optional[dict]:
        spec = spack.spec.parse_with_version_concrete(spec)
        extra_attributes = getattr(spec, _EXTRA_ATTRIBUTES_KEY, None)
        if extra_attributes is None:
            return None

        paths = CompilerConfigFactory._extract_compiler_paths(spec)
        if paths is None:
            return None

        compiler_spec = spack.spec.CompilerSpec(
            package_name_to_compiler_name.get(spec.name, spec.name), spec.version
        )

        operating_system, target = CompilerConfigFactory._extract_os_and_target(spec)

        compiler_entry = {
            "compiler": {
                "spec": str(compiler_spec),
                "paths": paths,
                "flags": extra_attributes.get("flags", {}),
                "operating_system": str(operating_system),
                "target": str(target.family),
                "modules": getattr(spec, "external_modules", []),
                "environment": extra_attributes.get("environment", {}),
                "extra_rpaths": extra_attributes.get("extra_rpaths", []),
                "implicit_rpaths": extra_attributes.get("implicit_rpaths", None),
            }
        }
        return compiler_entry

    @staticmethod
    def _extract_compiler_paths(spec: "spack.spec.Spec") -> Optional[Dict[str, str]]:
        err_header = f"The external spec '{spec}' cannot be used as a compiler"
        extra_attributes = spec.extra_attributes
        # If I have 'extra_attributes' warn if 'compilers' is missing,
        # or we don't have a C compiler
        if _COMPILERS_KEY not in extra_attributes:
            warnings.warn(
                f"{err_header}: missing the '{_COMPILERS_KEY}' key under '{_EXTRA_ATTRIBUTES_KEY}'"
            )
            return None
        attribute_compilers = extra_attributes[_COMPILERS_KEY]

        if _C_KEY not in attribute_compilers:
            warnings.warn(
                f"{err_header}: missing the C compiler path under "
                f"'{_EXTRA_ATTRIBUTES_KEY}:{_COMPILERS_KEY}'"
            )
            return None
        c_compiler = attribute_compilers[_C_KEY]

        # C++ and Fortran compilers are not mandatory, so let's just leave a debug trace
        if _CXX_KEY not in attribute_compilers:
            tty.debug(f"[{__file__}] The external spec {spec} does not have a C++ compiler")

        if _FORTRAN_KEY not in attribute_compilers:
            tty.debug(f"[{__file__}] The external spec {spec} does not have a Fortran compiler")

        # compilers format has cc/fc/f77, externals format has "c/fortran"
        return {
            "cc": c_compiler,
            "cxx": attribute_compilers.get(_CXX_KEY, None),
            "fc": attribute_compilers.get(_FORTRAN_KEY, None),
            "f77": attribute_compilers.get(_FORTRAN_KEY, None),
        }

    @staticmethod
    def _extract_os_and_target(spec: "spack.spec.Spec"):
        if not spec.architecture:
            host_platform = spack.platforms.host()
            operating_system = host_platform.operating_system("default_os")
            target = host_platform.target("default_target").microarchitecture
        else:
            target = spec.architecture.target
            if not target:
                target = spack.platforms.host().target("default_target")
            target = target.microarchitecture

            operating_system = spec.os
            if not operating_system:
                host_platform = spack.platforms.host()
                operating_system = host_platform.operating_system("default_os")
        return operating_system, target


class InvalidCompilerConfigurationError(spack.error.SpackError):
    def __init__(self, compiler_spec):
        super().__init__(
            'Invalid configuration for [compiler "%s"]: ' % compiler_spec,
            "Compiler configuration must contain entries for all compilers: %s"
            % _path_instance_vars,
        )


class NoCompilersError(spack.error.SpackError):
    def __init__(self):
        super().__init__("Spack could not find any compilers!")


class UnknownCompilerError(spack.error.SpackError):
    def __init__(self, compiler_name):
        super().__init__("Spack doesn't support the requested compiler: {0}".format(compiler_name))


class NoCompilerForSpecError(spack.error.SpackError):
    def __init__(self, compiler_spec, target):
        super().__init__(
            "No compilers for operating system %s satisfy spec %s" % (target, compiler_spec)
        )


class CompilerDuplicateError(spack.error.SpackError):
    def __init__(self, compiler_spec, arch_spec):
        config_file_to_duplicates = get_compiler_duplicates(compiler_spec, arch_spec)
        duplicate_table = list((x, len(y)) for x, y in config_file_to_duplicates.items())
        descriptor = lambda num: "time" if num == 1 else "times"
        duplicate_msg = lambda cfgfile, count: "{0}: {1} {2}".format(
            cfgfile, str(count), descriptor(count)
        )
        msg = (
            "Compiler configuration contains entries with duplicate"
            + " specification ({0}, {1})".format(compiler_spec, arch_spec)
            + " in the following files:\n\t"
            + "\n\t".join(duplicate_msg(x, y) for x, y in duplicate_table)
        )
        super().__init__(msg)


class CompilerSpecInsufficientlySpecificError(spack.error.SpackError):
    def __init__(self, compiler_spec):
        super().__init__("Multiple compilers satisfy spec %s" % compiler_spec)
