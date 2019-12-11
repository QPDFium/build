#!/usr/bin/env python
#
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Process Android resource directories to generate .resources.zip, R.txt and
.srcjar files."""

import argparse
import collections
import os
import re
import shutil
import sys
import zipfile

from util import build_utils
from util import manifest_utils
from util import md5_check
from util import resource_utils

_AAPT_IGNORE_PATTERN = ':'.join([
    '*OWNERS',  # Allow OWNERS files within res/
    '*.py',  # PRESUBMIT.py sometimes exist.
    '*.pyc',
    '*~',  # Some editors create these as temp files.
    '.*',  # Never makes sense to include dot(files/dirs).
    '*.d.stamp', # Ignore stamp files
    ])

def _ParseArgs(args):
  """Parses command line options.

  Returns:
    An options object as from argparse.ArgumentParser.parse_args()
  """
  parser, input_opts, output_opts = resource_utils.ResourceArgsParser()

  input_opts.add_argument(
      '--aapt-path', required=True, help='Path to the Android aapt tool')

  input_opts.add_argument(
      '--res-sources-path',
      required=True,
      help='Path to a list of input resources for this target.')

  input_opts.add_argument(
      '--shared-resources',
      action='store_true',
      help='Make resources shareable by generating an onResourcesLoaded() '
           'method in the R.java source file.')

  input_opts.add_argument('--custom-package',
                          help='Optional Java package for main R.java.')

  input_opts.add_argument(
      '--android-manifest',
      help='Optional AndroidManifest.xml path. Only used to extract a package '
           'name for R.java if a --custom-package is not provided.')

  output_opts.add_argument(
      '--resource-zip-out',
      help='Path to a zip archive containing all resources from '
      '--resource-dirs, merged into a single directory tree.')

  output_opts.add_argument('--srcjar-out',
                    help='Path to .srcjar to contain the generated R.java.')

  output_opts.add_argument('--r-text-out',
                    help='Path to store the generated R.txt file.')

  input_opts.add_argument(
      '--strip-drawables',
      action="store_true",
      help='Remove drawables from the resources.')

  options = parser.parse_args(args)

  resource_utils.HandleCommonOptions(options)

  with open(options.res_sources_path) as f:
    options.sources = [line.strip() for line in f.readlines()]
  options.resource_dirs = resource_utils.ExtractResourceDirsFromFileList(
      options.sources)

  return options


def _CheckAllFilesListed(resource_files, resource_dirs):
  resource_files = set(resource_files)
  missing_files = []
  for path, _ in resource_utils.IterResourceFilesInDirectories(resource_dirs):
    if path not in resource_files:
      missing_files.append(path)

  if missing_files:
    sys.stderr.write('Error: Found files not listed in the sources list of '
                     'the BUILD.gn target:\n')
    for path in missing_files:
      sys.stderr.write('{}\n'.format(path))
    sys.exit(1)


def _ZipResources(resource_dirs, zip_path, ignore_pattern):
  # Python zipfile does not provide a way to replace a file (it just writes
  # another file with the same name). So, first collect all the files to put
  # in the zip (with proper overriding), and then zip them.
  # ignore_pattern is a string of ':' delimited list of globs used to ignore
  # files that should not be part of the final resource zip.
  files_to_zip = dict()
  files_to_zip_without_generated = dict()
  for index, resource_dir in enumerate(resource_dirs):
    for path, archive_path in resource_utils.IterResourceFilesInDirectories(
        [resource_dir], ignore_pattern):
      resource_dir_name = os.path.basename(resource_dir)
      archive_path = '{}_{}/{}'.format(index, resource_dir_name, archive_path)
      # We want the original resource dirs in the .info file rather than the
      # generated overridden path.
      if not path.startswith('/tmp'):
        files_to_zip_without_generated[archive_path] = path
      files_to_zip[archive_path] = path
  resource_utils.CreateResourceInfoFile(files_to_zip_without_generated,
                                        zip_path)
  with zipfile.ZipFile(zip_path, 'w') as z:
    # This magic comment signals to resource_utils.ExtractDeps that this zip is
    # not just the contents of a single res dir, without the encapsulating res/
    # (like the outputs of android_generated_resources targets), but instead has
    # the contents of possibly multiple res/ dirs each within an encapsulating
    # directory within the zip.
    z.comment = resource_utils.MULTIPLE_RES_MAGIC_STRING
    build_utils.DoZip(files_to_zip.iteritems(), z)


def _GenerateRTxt(options, dep_subdirs, gen_dir):
  """Generate R.txt file.

  Args:
    options: The command-line options tuple.
    dep_subdirs: List of directories containing extracted dependency resources.
    gen_dir: Locates where the aapt-generated files will go. In particular
      the output file is always generated as |{gen_dir}/R.txt|.
  """
  # NOTE: This uses aapt rather than aapt2 because 'aapt2 compile' does not
  # support the --output-text-symbols option yet (https://crbug.com/820460).
  package_command = [
      options.aapt_path,
      'package',
      '-m',
      '-M',
      manifest_utils.EMPTY_ANDROID_MANIFEST_PATH,
      '--no-crunch',
      '--auto-add-overlay',
      '--no-version-vectors',
  ]
  for j in options.include_resources:
    package_command += ['-I', j]

  ignore_pattern = resource_utils.AAPT_IGNORE_PATTERN
  if options.strip_drawables:
    ignore_pattern += ':*drawable*'
  package_command += [
      '--output-text-symbols',
      gen_dir,
      '-J',
      gen_dir,  # Required for R.txt generation.
      '--ignore-assets',
      ignore_pattern
  ]

  # Adding all dependencies as sources is necessary for @type/foo references
  # to symbols within dependencies to resolve. However, it has the side-effect
  # that all Java symbols from dependencies are copied into the new R.java.
  # E.g.: It enables an arguably incorrect usage of
  # "mypackage.R.id.lib_symbol" where "libpackage.R.id.lib_symbol" would be
  # more correct. This is just how Android works.
  for d in dep_subdirs:
    package_command += ['-S', d]

  for d in options.resource_dirs:
    package_command += ['-S', d]

  # Only creates an R.txt
  build_utils.CheckOutput(
      package_command, print_stdout=False, print_stderr=False)


def _GenerateResourcesZip(output_resource_zip, input_resource_dirs,
                          strip_drawables):
  """Generate a .resources.zip file fron a list of input resource dirs.

  Args:
    output_resource_zip: Path to the output .resources.zip file.
    input_resource_dirs: A list of input resource directories.
  """

  ignore_pattern = resource_utils.AAPT_IGNORE_PATTERN
  if strip_drawables:
    ignore_pattern += ':*drawable*'
  _ZipResources(input_resource_dirs, output_resource_zip, ignore_pattern)


def _OnStaleMd5(options):
  with resource_utils.BuildContext() as build:
    if options.sources:
      _CheckAllFilesListed(options.sources, options.resource_dirs)
    if options.r_text_in:
      r_txt_path = options.r_text_in
    else:
      # Extract dependencies to resolve @foo/type references into
      # dependent packages.
      dep_subdirs = resource_utils.ExtractDeps(options.dependencies_res_zips,
                                               build.deps_dir)

      _GenerateRTxt(options, dep_subdirs, build.gen_dir)
      r_txt_path = build.r_txt_path

      # 'aapt' doesn't generate any R.txt file if res/ was empty.
      if not os.path.exists(r_txt_path):
        build_utils.Touch(r_txt_path)

    if options.r_text_out:
      shutil.copyfile(r_txt_path, options.r_text_out)

    if options.srcjar_out:
      package = options.custom_package
      if not package and options.android_manifest:
        _, manifest_node, _ = manifest_utils.ParseManifest(
            options.android_manifest)
        package = manifest_utils.GetPackage(manifest_node)

      # Don't create a .java file for the current resource target when no
      # package name was provided (either by manifest or build rules).
      if package:
        # All resource IDs should be non-final here, but the
        # onResourcesLoaded() method should only be generated if
        # --shared-resources is used.
        rjava_build_options = resource_utils.RJavaBuildOptions()
        rjava_build_options.ExportAllResources()
        rjava_build_options.ExportAllStyleables()
        if options.shared_resources:
          rjava_build_options.GenerateOnResourcesLoaded()

        # Not passing in custom_root_package_name or parent to keep
        # file names unique.
        resource_utils.CreateRJavaFiles(
            build.srcjar_dir, package, r_txt_path, options.extra_res_packages,
            options.extra_r_text_files, rjava_build_options, options.srcjar_out)

      build_utils.ZipDir(options.srcjar_out, build.srcjar_dir)

    if options.resource_zip_out:
      _GenerateResourcesZip(options.resource_zip_out, options.resource_dirs,
                            options.strip_drawables)


def main(args):
  args = build_utils.ExpandFileArgs(args)
  options = _ParseArgs(args)

  # Order of these must match order specified in GN so that the correct one
  # appears first in the depfile.
  possible_output_paths = [
    options.resource_zip_out,
    options.r_text_out,
    options.srcjar_out,
  ]
  output_paths = [x for x in possible_output_paths if x]

  # List python deps in input_strings rather than input_paths since the contents
  # of them does not change what gets written to the depsfile.
  input_strings = options.extra_res_packages + [
      options.custom_package,
      options.shared_resources,
      options.strip_drawables,
  ]

  possible_input_paths = [
    options.aapt_path,
    options.android_manifest,
  ]
  possible_input_paths += options.include_resources
  input_paths = [x for x in possible_input_paths if x]
  input_paths.extend(options.dependencies_res_zips)
  input_paths.extend(options.extra_r_text_files)

  # Resource files aren't explicitly listed in GN. Listing them in the depfile
  # ensures the target will be marked stale when resource files are removed.
  depfile_deps = []
  resource_names = []
  for resource_dir in options.resource_dirs:
    for resource_file in build_utils.FindInDirectory(resource_dir, '*'):
      # Don't list the empty .keep file in depfile. Since it doesn't end up
      # included in the .zip, it can lead to -w 'dupbuild=err' ninja errors
      # if ever moved.
      if not resource_file.endswith(os.path.join('empty', '.keep')):
        input_paths.append(resource_file)
        depfile_deps.append(resource_file)
      resource_names.append(os.path.relpath(resource_file, resource_dir))

  # Resource filenames matter to the output, so add them to strings as well.
  # This matters if a file is renamed but not changed (http://crbug.com/597126).
  input_strings.extend(sorted(resource_names))

  md5_check.CallAndWriteDepfileIfStale(
      lambda: _OnStaleMd5(options),
      options,
      input_paths=input_paths,
      input_strings=input_strings,
      output_paths=output_paths,
      depfile_deps=depfile_deps)


if __name__ == '__main__':
  main(sys.argv[1:])
