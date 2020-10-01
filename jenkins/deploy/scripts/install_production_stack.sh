#!/bin/bash -l
set -euo pipefail

# This script assumes that the following variables are set in the environment:
#
# SENV_VIRTUALENV_PATH: path where to find the virtualenv for "senv"
#

environment=$1

set +e
rm -f stack.${environment}.xml
set -e

set +u
. ${SENV_VIRTUALENV_PATH}/bin/activate
set -u

SPACK_CHECKOUT_DIR=$(senv --input ${STACK_RELEASE}.yaml spack-checkout-dir)

if [ x'${DRY_RUN}' = 'xyes' ]; then
    SPACK="echo ${SPACK_CHECKOUT_DIR}/bin/spack"
    SENV="echo senv"
else
    SPACK="${SPACK_CHECKOUT_DIR}/bin/spack"
    SENV="senv"
fi


${SPACK} --env ${environment} install --log-format=junit --log-file=stack.${environment}.xml

${SPACK} --env ${environment} module lmod refresh -y
