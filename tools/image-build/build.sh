#!/usr/bin/env bash

# Prerequisites:
#   * sudo apt install -y qemu git kpartx debootstrap

set -e

which pip || curl https://bootstrap.pypa.io/get-pip.py | sudo python -
sudo pip install diskimage-builder

TROVE_REPO_PATH=${TROVE_REPO_PATH:-"/opt/stack/trove"}
# Figure out where our directory is located
if [ -z $TROVE_REPO_PATH ]; then
    BUILD_DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && cd .. && pwd )
    TROVE_REPO_PATH=${TROVE_REPO_PATH:-${BUILD_DIR%/*}}
fi

BUILD_LOGFILE=${BUILD_LOGFILE:-"/var/log/dib-build/trove-guest-agent.log"}
GUEST_CACHEDIR=${GUEST_CACHEDIR:-"$HOME/.cache/image-create"}
GUEST_BASEOS=${GUEST_BASEOS:-"ubuntu"}
GUEST_RELEASE=${GUEST_RELEASE:-"xenial"}
DEV_MODE=${DEV_MODE:-"false"}
ELEMENTS=${ELEMENTS:-"base vm"}
export SERVICE_TYPE=${SERVICE_TYPE:-"mysql"}
export GUEST_USERNAME=${GUEST_USERNAME:-"ubuntu"}
export HOST_USERNAME=${HOST_USERNAME:-"ubuntu"}
export SSH_DIR=${SSH_DIR:-"$HOME/.ssh"}

GUEST_OUTPUTFILENAME=${GUEST_OUTPUTFILENAME:-"$PWD/trove-guest-image"}
GUEST_IMAGETYPE=${GUEST_IMAGETYPE:-"qcow2"}
GUEST_IMAGESIZE=${GUEST_IMAGESIZE:-3}
trove_elements_path=${TROVE_REPO_PATH}/integration/scripts/files/elements

# Trove doesn't support to specify keypair when creating the db instance, the
# ssh keys are injected when the image is built. This could be removed when
# we support keypair in the future.
if [ -d ${SSH_DIR} ]; then
    cat ${SSH_DIR}/id_rsa.pub >> ${SSH_DIR}/authorized_keys
    sort ${SSH_DIR}/authorized_keys | uniq > ${SSH_DIR}/authorized_keys.uniq
    mv ${SSH_DIR}/authorized_keys.uniq ${SSH_DIR}/authorized_keys
else
    mkdir -p ${SSH_DIR}
    /usr/bin/ssh-keygen -f ${SSH_DIR}/id_rsa -q -N ""
    cat ${SSH_DIR}/id_rsa.pub >> ${SSH_DIR}/authorized_keys
    chmod 600 ${SSH_DIR}/authorized_keys
fi

if [[ "${DEV_MODE}" == "true" ]]; then
    host_ip=$(ip route get 8.8.8.8 | head -1 | awk '{print $7}')
    export CONTROLLER_IP=${CONTROLLER_IP:-${host_ip}}
    export HOST_SCP_USERNAME=${HOST_SCP_USERNAME:-"ubuntu"}
    export PATH_TROVE=${TROVE_REPO_PATH}
    export ESCAPED_PATH_TROVE=$(echo ${PATH_TROVE} | sed 's/\//\\\//g')
    export GUEST_LOGDIR=${GUEST_LOGDIR:-"/var/log/trove/"}
    export ESCAPED_GUEST_LOGDIR=$(echo ${GUEST_LOGDIR} | sed 's/\//\\\//g')
    export TROVESTACK_SCRIPTS=${TROVE_REPO_PATH}/integration/scripts

    # Make sure the guest agent has permission to ssh into the devstack host
    # in order to download trove code during the service initialization, so we
    # need to add the public key in SSH_DIR folder into the current user's
    # authorized_keys
    home_keys=/home/${HOST_USERNAME}/.ssh/authorized_keys
    cat ${SSH_DIR}/id_rsa.pub >> ${home_keys}
    sort ${home_keys} | uniq > ${home_keys}.uniq
    mv ${home_keys}.uniq ${home_keys}
fi

# For system-wide installs, DIB will automatically find the elements, so we only check local path
if [ "${DIB_LOCAL_ELEMENTS_PATH}" ]; then
    export ELEMENTS_PATH=${trove_elements_path}:${DIB_LOCAL_ELEMENTS_PATH}
else
    export ELEMENTS_PATH=${trove_elements_path}
fi

export DIB_RELEASE=${GUEST_RELEASE}
export DIB_CLOUD_INIT_DATASOURCES="ConfigDrive"

# Find out what platform we are on
if [ -e /etc/os-release ]; then
    platform=$(head -1 /etc/os-release)
else
    platform=$(head -1 /etc/system-release | grep -e CentOS -e 'Red Hat Enterprise Linux' || :)
    if [ -z "$platform" ]; then
        echo -e "Unknown Host OS. Impossible to build images.\nAborting"
        exit 2
    fi
fi

# Make sure we have the required packages installed
if [ "$platform" = 'NAME="Ubuntu"' ]; then
    PKG_LIST="qemu git"
    for pkg in $PKG_LIST; do
        if ! dpkg --get-selections | grep -q "^$pkg[[:space:]]*install$" >/dev/null; then
            echo "Required package " $pkg " is not installed."
            exit 1
        fi
    done
fi

if  [ "${GUEST_WORKING_DIR}" ]; then
    mkdir -p ${GUEST_WORKING_DIR}
    TEMP=$(mktemp -d ${GUEST_WORKING_DIR}/diskimage-create.XXXXXX)
else
    TEMP=$(mktemp -d diskimage-create.XXXXXX)
fi
pushd $TEMP > /dev/null

ELEMENTS="$ELEMENTS ${GUEST_BASEOS}"

if [[ "${DEV_MODE}" != "true" ]]; then
    ELEMENTS="$ELEMENTS pip-and-virtualenv"
    ELEMENTS="$ELEMENTS pip-cache"
    ELEMENTS="$ELEMENTS no-resolvconf"
    ELEMENTS="$ELEMENTS guest-agent"
else
    ELEMENTS="$ELEMENTS ${GUEST_BASEOS}-guest"
    ELEMENTS="$ELEMENTS ${GUEST_BASEOS}-${GUEST_RELEASE}-guest"
fi

ELEMENTS="$ELEMENTS ${GUEST_BASEOS}-${SERVICE_TYPE}"
ELEMENTS="$ELEMENTS ${GUEST_BASEOS}-${GUEST_RELEASE}-${SERVICE_TYPE}"

# Build the image
disk-image-create --logfile=${BUILD_LOGFILE} \
  -x \
  -a amd64 \
  -o ${GUEST_OUTPUTFILENAME} \
  -t ${GUEST_IMAGETYPE} \
  --image-size ${GUEST_IMAGESIZE} \
  --image-cache ${GUEST_CACHEDIR} \
  $ELEMENTS

# out of $TEMP
popd > /dev/null
rm -rf $TEMP

echo "Image ${GUEST_OUTPUTFILENAME}.${GUEST_IMAGETYPE} was built successfully."
