kind: junction

sources:
- kind: git_repo
  url: gitlab:freedesktop-sdk/freedesktop-sdk.git
  ref: freedesktop-sdk-25.08.14-0-g57149392fe26548b0e7c50a2e171e3aac005a412

config:
  options:
    target_arch: '%{arch}'
    bootstrap_build_arch: '%{arch}'
