# Driven by the Makefile in this directory, which supplies -B and -D SDKCONFIG
# so that each BOARD gets its own build tree. Nothing here is chip-specific:
# `idf.py set-target` picks the toolchain and pulls in the matching
# sdkconfig.defaults.<target> fragment.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(MDP_Adapter_Multiceiver)
