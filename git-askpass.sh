#!/bin/sh

case "$1" in
  *Username*) printf '%s\n' "${GIT_USERNAME:-x-access-token}" ;;
  *Password*) printf '%s\n' "${GIT_TOKEN:-}" ;;
  *) exit 1 ;;
esac
