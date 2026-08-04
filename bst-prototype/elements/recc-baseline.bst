kind: manual

description: |
  Deterministic C++ compiler pilot for comparing BuildStream and RECC.
  The project option controls the compiler launcher and RECC mode.

sources:
- kind: local
  path: files/recc-baseline

build-depends:
- freedesktop-sdk.bst:components/gcc.bst

variables:
  compiler: /usr/bin/g++
  compiler_launcher: ""
  (?):
  - recc != "buildstream-only":
      compiler_launcher: recc

environment:
  CXX: "%{compiler}"
  CXX_LAUNCHER: "%{compiler_launcher}"
  (?):
  - recc != "buildstream-only":
      RECC_PROJECT_ROOT: /
      RECC_SERVER: grpc://frontend.buildbarn.svc.cluster.local:8980
      RECC_CAS_SERVER: grpc://frontend.buildbarn.svc.cluster.local:8980
      RECC_ACTION_CACHE_SERVER: grpc://frontend.buildbarn.svc.cluster.local:8980
      RECC_ACTION_UNCACHEABLE: "0"
      RECC_ENABLE_METRICS: "1"
      RECC_METRICS_FILE: "%{build-root}/recc-metrics.txt"
  - recc == "passthrough":
      RECC_PASSTHROUGH: "1"
  - recc == "cache-only":
      RECC_CACHE_ONLY: "1"
  - recc == "upload-local-build":
      RECC_CACHE_ONLY: "1"
      RECC_CACHE_UPLOAD_LOCAL_BUILD: "1"

config:
  build-commands:
  - |
    set -eu
    mkdir -p "%{build-root}"
    mkdir -p "%{install-root}"
    run_cxx() {
      if [ -n "${CXX_LAUNCHER}" ]; then
        "${CXX_LAUNCHER}" "${CXX}" "$@"
      else
        "${CXX}" "$@"
      fi
    }
    run_cxx -std=c++17 -O2 -g0 -fno-ident -c message.cpp \
      -o "%{build-root}/message.o"
    run_cxx -std=c++17 -O2 -g0 -fno-ident -c main.cpp \
      -o "%{build-root}/main.o"
    run_cxx -std=c++17 -Wl,--build-id=none \
      "%{build-root}/message.o" "%{build-root}/main.o" \
      -o "%{build-root}/recc-baseline"
    if [ -f "%{build-root}/recc-metrics.txt" ]; then
      while IFS= read -r METRIC; do
        printf '[RECC_METRICS] %s\n' "${METRIC}"
      done < "%{build-root}/recc-metrics.txt"
      rm -f "%{build-root}/recc-metrics.txt"
    fi
  install-commands:
  - |
    set -eu
    mkdir -p "%{install-root}/usr/bin"
    install -m 0755 "%{build-root}/recc-baseline" \
      "%{install-root}/usr/bin/recc-baseline"
