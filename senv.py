#!/usr/bin/env python3

# This script creates multiple spack environment files from a template
#
# Input sources:
#  - a Jinja2 template (spack.yaml.j2)
#  - a YAML file containing (meleze.yaml)
#     - the environments to create
#     - variables to be written per environment
#
# This file and the input live in the spack-site repo
#
from __future__ import print_function
import os
import re
import copy
import click
import datetime
import jinja2
import yaml
import shutil
import git
from collections import MutableMapping
import subprocess
try:
    from subprocess import DEVNULL # py3k
except ImportError:
    DEVNULL = open(os.devnull, 'wb')

class CloneProgress(git.RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        if message:
            print(message)


def _compiler(value, component='cc'):
    _compilers = {
        'intel': { 'cc': 'icc',
                   'c++': 'icpc',
                   'f77': 'ifort',
                   'f90': 'ifort'},
        'gcc': { 'cc': 'gcc',
                 'c++': 'g++',
                 'f77': 'gfortran',
                 'f90': 'gfortran'},
        'clang': { 'cc': 'clang',
                   'c++': 'clang++',
                   'f77': 'flang',
                   'f90': 'flang'},
        'nvhpc': { 'cc': 'nvc',
                   'c++': 'nvc++',
                   'f77': 'nvfortran',
                   'f90': 'nvfortran'}}
    return _compilers[value][component]

def _absolute_path(value, prefix=None):
    if os.path.isabs(value):
        return value
    if prefix is None:
        return os.path.abspath(value)
    if isinstance(prefix, list):
        prefix.append(value)
        return os.path.join(*prefix)
    return os.path.join(prefix, value)


def _filter_variant(value):
    variant = re.compile('[ +~^][^+~\^]+')
    if isinstance(value, str):
        return variant.sub("", value)
    return [ variant.sub("", v) for v in value ]

# Custom filter method
def _regex_replace(s, find, replace):
    """A non-optimal implementation of a regex filter"""
    ns = re.sub(find, replace, s)
    return ns

def _cuda_variant(environment, arch=True,
                  extra_off='', extra_on='',
                  stack='stable',
                  dep=False):
    if 'gpu' not in environment or environment['gpu'] != 'nvidia':
        return '~cuda{}'.format(extra_off)

    variant = "+cuda"
    if arch:
        variant = '{0} cuda_arch={1}'.format(
            variant,
            environment[stack]['cuda']['arch'].replace('sm_', '')
        )
        variant = "{0} {1}".format(variant, extra_on)
    if dep:
        variant = '{0} ^{1}'.format(
            variant,
            environment[stack]['cuda']['package'])

    return variant

def _hip_variant(environment, arch=True,
                  extra_off='', extra_on='',
                  stack='stable',
                  dep=False):
    if 'gpu' not in environment or environment['gpu'] != 'amd':
        return '~hip{}'.format(extra_off)

    variant = '+hip{}'.format(extra_on)
    if arch:
        variant = '{0} amd_gpu_arch={1}'.format(
            variant,
            environment[stack]['rocm']['arch']
        )


    return variant


class SpackEnvs(object):
    def __init__(self, configuration, prefix=None):
        self.configuration = configuration
        self.environments = self.configuration.pop('environments')

        info_message='This file was created by magic at {0}'.format(
            datetime.datetime.now().strftime("%x %X"))

        self.customisation = dict()
        self.customisation['environment'] = \
            self.configuration['default_environment']
        self.customisation["info_message"] = info_message
        self.customisation["warning"] = 'DO NOT EDIT THIS FILE DIRECTLY'

        for k, v in self.configuration.items():
            self.customisation[k] = v

        if prefix is None:
            prefix = self.configuration['spack_root']

        if 'stack_release' in self.configuration and 'stack_version' in self.configuration:
            self.spack_source_root = os.path.join(
                prefix,
                self.configuration['stack_release'],
                'spack.{0}'.format(self.configuration['stack_version']))
            self.spack_install_root = os.path.join(
                prefix,
                self.configuration['stack_release'],
                self.configuration['stack_version'])
        else:
            self.spack_source_root = os.path.join(prefix, 'spack')
            self.spack_install_root = os.path.join(prefix, 'spack')

        self.spack_environment_root = os.path.join(
            self.spack_source_root,
            "var", "spack", "environments")

        # Creating Jinja2 environment
        self.spack_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader('./'),
            trim_blocks=True, lstrip_blocks=True,
            extensions=[],
            undefined=jinja2.DebugUndefined
        )

        # Registering custom filters
        self.spack_env.filters['exists'] = os.path.exists
        self.spack_env.filters['list_if_not'] = \
            lambda x: x if isinstance(x, list) else [x]
        self.spack_env.filters['filter_variant'] = _filter_variant
        self.spack_env.filters['compiler'] = _compiler
        self.spack_env.filters['absolute_path'] = _absolute_path
        self.spack_env.filters['regex_replace'] = _regex_replace
        self.spack_env.globals['cuda_variant'] = _cuda_variant
        self.spack_env.globals['hip_variant'] = _hip_variant

    def _create_jinja_environment(self, template_path=None):
        if template_path is None:
            template_path = os.path.join('templates', 'common', 'spack.yaml.j2')
        return self.spack_env.get_template(template_path)

    def _dict_merge(self, d1, d2):
        '''
        Update two dicts of dicts recursively,
        if either mapping has leaves that are non-dicts,
        the second's leaf overwrites the first's.
        '''
        for k, v in d1.items(): # in Python 2, use .iteritems()!
            if k in d2:
                # this next check is the only difference!
                if all(isinstance(e, MutableMapping) for e in (v, d2[k])):
                    d2[k] = self._dict_merge(v, d2[k])
                # we could further check types and merge as appropriate here.
        d3 = d1.copy()
        d3.update(d2)
        return d3

    # get a cache for a given operation
    def _get_cache(self, type_):
        class cache(object):
            def __init__(self, type_, config):
                
                self.cache_file = os.path.expanduser('~/.{0}{1}_{2}_cache.yaml'.format(
                    config['stack_release'],
                    '.{0}'.format(config['stack_version']) if 'stack_version' in config else '',
                    type_))

                try:
                    with open(self.cache_file, 'r') as fh:
                        self.cache = yaml.load(fh, Loader=yaml.FullLoader)
                except IOError:
                    self.cache = None
                    pass

            def save(self):
                with open(self.cache_file, 'w') as fh:
                    yaml.dump(self.cache, fh)

        return cache(type_, self.configuration)

    # get the environment dict overriding the configurations
    # if there are environment specific ones
    def _get_env_customisation(self, environment):
        if environment not in self.environments and environment is not None:
            raise RuntimeError(
                'The environment {0} is not defined.'
                ' Valid environments are {1}'.format(environment,
                                                     self.list_envs(all=True)))
        customisation = copy.copy(self.customisation)

        customisation["environment"]['name'] = environment
        if environment is None:
            customisation["environment"]['name'] = 'None'

        # create a dictionary for each environment
        env = customisation['environment']
        if environment in self.configuration:
            customisation['environment'] = self._dict_merge(
                customisation['environment'],
                self.configuration[environment])

        # adds the compiler prefixes if they do not exists
        cache = self._get_cache('compilers')
        if cache.cache is None:
            cache.cache = {}
        for _type in customisation['environment']['stack_types']:
            if _type not in customisation['environment']:
                continue

            for compiler in customisation['environment'][_type]:
                stack = customisation['environment'][_type][compiler]
                if 'compiler' not in stack or 'compiler_prefix' in stack:
                    continue
                
                spec_compiler = self._compiler_name(
                    stack['compiler'],
                    customisation,
                    stack=customisation['environment'][_type]
                )

                if spec_compiler in cache.cache:
                    spack_path = cache.cache[spec_compiler]
                else:
                    spack_path = self._spack_path(spec_compiler,
                                                  environment)

                if spack_path is not None:
                    customisation['environment'][_type][compiler]['compiler_prefix'] = spack_path
                    cache.cache[spec_compiler] = spack_path
        cache.save()
        return customisation

    def _compiler_name(self, compiler, customisation, stack=None, core_compiler=None):
        compiler_ = copy.copy(compiler)

        nvptx_re = re.compile('.*\+nvptx')
        if stack is not None and nvptx_re.match(compiler) and 'cuda' in stack:
            compiler_ = '{0} ^{1}'.format(compiler, stack['cuda']['package'])

        if '%' in compiler_:
            return compiler_

        return '{0} %{1}'.format(
            compiler_,
            core_compiler if core_compiler is not None
                          else customisation['environment']['core_compiler'])

    def _run_spack(self, *args, **kwargs):
        environment = kwargs.pop('environment', None)
        no_wait = kwargs.pop('no_wait', False)
        options = { 'stdout': subprocess.PIPE,
                    'stderr': DEVNULL }
        if environment is not None:
            options['env'] = {'SPACK_ENV': os.path.join(
                self.spack_environment_root,
                environment)}

        command = [os.path.join(self.spack_source_root, 'bin', 'spack')]
        command.extend(args)

        spack = subprocess.Popen(command, **options)

        return spack

    def _spack_path(self, value, environment):
        spack = self._run_spack('find', '--paths', value,
                                environment=environment)

        path_re = re.compile('.*(({0}|{1}).*)$'.format(
            self.spack_install_root,
            _absolute_path(self.configuration['spack_external'],
                           prefix=self.configuration['spack_root'])))

        for line in spack.stdout:
            match = path_re.match(line.decode('ascii'))
            if match:
                return match.group(1)

        return None

    def compilers(self, environment, stack_type=None, all=False):
        compilers = []
        customisation = self._get_env_customisation(environment)
        if stack_type is not None:
            stack_types = [stack_type]
        else:
            stack_types = customisation['environment']['stack_types']
        environments = [customisation['environment']]
        if all:
            environments.append(self._get_env_customisation(None)['environment'])

        for env in environments:
            for _type in stack_types:
                for name, stack in env[_type].items():
                    if 'compiler' in stack and name in \
                       customisation['environment']['compilers']:
                        compilers.append(self._compiler_name(
                            stack['compiler'],
                            customisation,
                            stack=customisation['environment'][_type],
                            core_compiler=customisation['environment']['core_compiler']))
        return list(set(compilers))

    def list_envs(self, cloud=None, all=False):
        if all:
            return self.environments
        envs = []
        for env in self.environments:
            customisation = self._get_env_customisation(env)
            if ((cloud is None
                and 'cloud' not in customisation['environment'])
                or (cloud is not None
                    and ('cloud' in customisation['environment']
                         and customisation['environment']['cloud'] == cloud))):
                envs.append(env)
        return envs

    def write_envs(self, bootstrap=False):
        for environment in self.environments:
            self.write_env(environment, bootstrap)

    def write_env(self, environment, bootstrap=False):
        spack_yaml_root = os.path.join(self.spack_environment_root,
                                       environment)
        print('Creating evironment {0}  in {1}'.format(environment,
                                                       spack_yaml_root))

        spack_env_template = self._create_jinja_environment()

        if not os.path.isdir(spack_yaml_root):
            raise RuntimeError(
                '{0} does not exists, please first'
                ' run spack env create {1}'.format(spack_yaml_root, environment)
            )

        customisation = self._get_env_customisation(environment)
        customisation['environment']['bootstrap'] = bootstrap
        with open(os.path.join(spack_yaml_root, 'spack.yaml'), 'w+') as f:
            f.write(spack_env_template.render(customisation))

    def spack_release(self):
        print(self.configuration['spack_release'])

    def spack_checkout_dir(self):
        print(self.spack_source_root)

    def spack_external_dir(self):
        print(_absolute_path(self.configuration['spack_external'],
                             prefix=self.configuration['spack_root']))

    def spack_checkout(self):
        if not os.path.exists(self.spack_source_root):
            git.Repo.clone_from('https://github.com/spack/spack.git', self.spack_source_root,
                                branch=self.configuration['spack_release'],
                                progress=CloneProgress())

    def spack_checkout_extra_repos(self):
        if 'extra_repos' not in self.configuration:
            return

        for repo in self.configuration['extra_repos']:
            info = self.configuration['extra_repos'][repo]
            repo_path = _absolute_path(info['path'],
                                       prefix=[self.configuration['spack_root'],
                                               self.configuration['stack_release'],
                                               'external_repos'])

            options={ 'progress': CloneProgress() }
            if os.path.exists(repo_path):
                repo = git.Repo(repo_path)
                repo.remotes.origin.pull(**options)
            else:
                if 'tag' in info:
                    options['branch'] = info['tag']
                    git.Repo.clone_from(info['repo'], repo_path, **options)

    def list_extra_repositories(self):
        repositories = []
        for item in self.configuration['extra_repos']:
            repo = self.configuration['extra_repos'][item]
            repo['name'] = item
            repo['path'] = _absolute_path(
                repo['path'],
                prefix=[self.configuration['spack_root'],
                        self.configuration['stack_release'],
                        'external_repos'])
            repositories.append(repo)
        print(yaml.dump(repositories))

    def install_spack_default_configuration(self):
        jinja_file_re = re.compile('(.*\.ya?ml)\.j2$')
        spack_config_path = os.path.join(self.spack_source_root, 'etc', 'spack')
        customisation = self._get_env_customisation(None)
        for _file in os.listdir('./configuration'):
            m = jinja_file_re.match(_file)
            template_path = os.path.join('./configuration', _file)
            if  m is not None:
                spack_env_template = self._create_jinja_environment(
                    template_path)
                with open(os.path.join(
                        spack_config_path, m.group(1)), 'w') as fh:
                    fh.write(spack_env_template.render(customisation))
            else:
                shutil.copyfile(
                    template_path,
                    os.path.join(spack_config_path, _file))

    def intel_compilers_configuration(self, environment):
        customisation = self._get_env_customisation(environment)
        jinja_file_re = re.compile('(.*\.cfg)\.j2$')
        for _type in customisation['environment']['stack_types']:
            if 'intel' in customisation['environment'][_type]:
                dict_ = customisation['environment'][_type]
                if 'external' in dict_['intel'] and dict_['intel']['external']:
                    intel_config_path = os.path.join(
                        dict_['intel']['compiler_prefix'], 'bin', 'intel64')
                else:
                    intel_config_path = os.path.join(
                        dict_['intel']['compiler_prefix'],
                        'compilers_and_libraries_{0}'.format(dict_['intel']['suite_version']),
                        'linux', 'bin', 'intel64')
                for _file in os.listdir('./external/intel/config'):
                    m = jinja_file_re.match(_file)
                    if not m:
                        continue
                    template_path = os.path.join('./external/intel/config', _file)
                    spack_env_template = self._create_jinja_environment(
                        template_path)
                    config_file = os.path.join(
                        intel_config_path, m.group(1))
                    with open(config_file, 'w') as fh:
                        fh.write(spack_env_template.render(dict_))
                        print('Writing file {}'.format(config_file))

    def spack_list_python(self, env, stack_type=None, installed_only=False):
        spack_config_path = os.path.join(self.spack_source_root, 'etc', 'spack')
        customisation = self._get_env_customisation(env)
        template_path = os.path.join('./templates/',
                                     self.configuration['site'],
                                     self.configuration['stack_release'])
        specs = []
        python_activated = {}
        for ver in [2, 3]:
            python_package_list = os.path.join(
                template_path,
                'python{}_activated.yaml.j2'.format('2' if ver == 2 else ''))
            if not  os.path.exists(python_package_list):
                continue
            
            python_activated[ver] = yaml.load(
                self._create_jinja_environment(
                    python_package_list
                ).render(customisation),
                Loader=yaml.FullLoader)
            
            if python_activated[ver] is None:
                python_activated[ver] = [] 

        if stack_type is not None:
            stack_types = [stack_type]
        else:
            stack_types = customisation['environment']['stack_types']

        installed_pkg_re = re.compile('[0-9a-z]* (.*?)@.*')

        for stack_type_ in stack_types:
            for compiler in customisation['environment'][stack_type_]:
                stack = customisation['environment'][stack_type_][compiler]
                if 'compiler' not in stack:
                    continue


                for ver in [2, 3]:
                    spec = {
                        'python_version': customisation['environment']['python'][ver],
                        'compiler': _filter_variant(stack['compiler']),
                        'arch': '',
                    }
                    if 'arch' in customisation['environment']:
                        spec['arch'] = ' arch={}'.format(customisation['environment']['arch'])

                    list_installed = []
                    if installed_only:
                        spack = self._run_spack('dependents', '--installed',
                                                'python@{python_version} %{compiler}{arch}'.format(**spec),
                                                environment=env)


                        for line in spack.stdout:
                            match = installed_pkg_re.match(line.decode('ascii'))
                            if match:
                                list_installed.append(match.group(1))

                    for package in python_activated[ver]:
                        if installed_only and package not in list_installed:
                            continue
                        
                        spec['pkg'] = package

                        specs.append('{pkg} ^python@{python_version} %{compiler}{arch}'.format(**spec))
        
        return specs            

    def activate_specs(self, environment, stack_type=None):
        specs = self.spack_list_python(environment, stack_type,
                                       installed_only=True)

        print("List of packages to activate:")
        for spec in specs:
            print(" - {}".format(spec))
        
        cache = self._get_cache('activated')
        if cache.cache is None:
            cache.cache = []

        for spec in specs:
            print("== Trying to activate {} ==".format(spec))
            if spec in cache.cache:
                print (' + ==> {0} activated [cache]'.format(spec))
                continue

            spack_ = self._run_spack('activate', spec, environment=environment)
            for line in spack_.stdout:
                print(' + {}'.format(line.decode('ascii')))

            if spack_.returncode is None:
                spack_.wait()

            if spack_.returncode == 0:
                print(" = Adding {} to cache".format(spec))
                cache.cache.append(spec)
        cache.save()

    def get_environment_entry(self, environment, entry):
        path = entry.split('.')
        customisation = self._get_env_customisation(environment)
        node = customisation
        for level in range(len(path) - 1):
            node = node[path[level]]

        if path[-1] in node:
            result = node[path[-1]]
            if not isinstance(result, str):
                print(yaml.dump(result))
            else:
                print(result)
        else:
            print('{0} was not specified in configuration'.format(entry))


@click.group()
@click.option(
    '--input', default='humagne.yaml', type=click.File('r'),
    help='YAML file containing the specification for a production environment')
@click.pass_context
def senv(ctx, input):
    """This command helps with common tasks needed in the SCITAS-EPFL
    continuous integration pipeline"""
    ctx.input = input
    ctx.configuration = yaml.load(input, Loader=yaml.FullLoader)

@senv.command()
def status():
    print("Senv ready to install stuff!")

@senv.command()
@click.option('--cloud', default=None)
@click.option('--all', default=False, is_flag=True)
@click.pass_context
def list_envs(ctxt, cloud, all):
    config = ctxt.parent.configuration
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    for env in spack_envs.list_envs(cloud=cloud, all=all):
        print('{}'.format(env))

@senv.command()
@click.option('--env', help='Environment to list the compiler for',
              default=None, required=False)
@click.option('--stack-type', help='Stack type: stable, bleeding_edge',
              default=None, required=False)
@click.option('--all', default=False, is_flag=True)
@click.pass_context
def list_compilers(ctxt, env, stack_type, all):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    compilers = spack_envs.compilers(env, stack_type, all)
    for compiler in compilers:
        print('{}'.format(compiler))

@senv.command()
@click.option('--env', help='Environment to create')
@click.option('--bootstrap',
              help='Create temporay environments to bootstrap',
              is_flag=True)
@click.pass_context
def create_env(ctxt, env, bootstrap):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.write_env(env, bootstrap=bootstrap)

@senv.command()
@click.option('--bootstrap',
              help='Create temporay environments to bootstrap',
              is_flag=True)
@click.pass_context
def create_envs(ctxt, bootstrap):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.write_envs(bootstrap=bootstrap)

@senv.command()
@click.pass_context
def spack_release(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.spack_release()

@senv.command()
@click.pass_context
def spack_checkout_dir(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.spack_checkout_dir()

@senv.command()
@click.pass_context
def spack_external_dir(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.spack_external_dir()

@senv.command()
@click.pass_context
def list_extra_repositories(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.list_extra_repositories()

@senv.command()
@click.pass_context
def install_spack_default_configuration(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.install_spack_default_configuration()

@senv.command()
@click.option('--env', help='Environment to list the compiler for')
@click.pass_context
def intel_compilers_configuration(ctxt, env):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.intel_compilers_configuration(env)

@senv.command()
@click.pass_context
def spack_checkout(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.spack_checkout()

@senv.command()
@click.pass_context
def spack_checkout_extra_repos(ctxt):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.spack_checkout_extra_repos()

@senv.command()
@click.option('--env', help='Environment to list the compiler for')
@click.option('--stack-type', help='Stack type: stable, bleeding_edge',
              default=None, required=False)
@click.pass_context
def list_spec_to_activate(ctxt, env, stack_type):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    specs = spack_envs.spack_list_python(env, stack_type, installed_only=True)
    for spec in specs:
        print(spec)


@senv.command()
@click.option('--env', help='Environment to list the compiler for')
@click.option('--stack-type', help='Stack type: stable, bleeding_edge',
              default=None, required=False)
@click.pass_context
def activate_specs(ctxt, env, stack_type):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.activate_specs(env, stack_type)

@senv.command()
@click.argument('entry', nargs=1)
@click.option('--env', help='Environment to list the compiler for',
              default=None)
@click.pass_context
def get_environment_entry(ctxt, entry, env):
    spack_envs = SpackEnvs(ctxt.parent.configuration)
    spack_envs.get_environment_entry(env, entry)
