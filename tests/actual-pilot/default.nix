# Public standalone check: no nix-secrets, cluster credentials, or live API.
{
  system ? builtins.currentSystem,
}:
let
  nixpkgs = builtins.fetchTree {
    type = "tarball";
    url = "https://releases.nixos.org/nixos/unstable/nixos-26.11pre1125864.2786a2a72088/nixexprs.tar.zst";
    narHash = "sha256-pAEASBc6wof0S58XcSEdZoQ2iFQDkCnZwH+wFLrkd1Q=";
  };
  pkgs = import nixpkgs { inherit system; };
  python = pkgs.python3.withPackages (p: [
    p.pyyaml
    p.jsonschema
  ]);
  celCheck = pkgs.buildGoModule {
    pname = "actual-pilot-cel-check";
    version = "1";
    src = ./cel-check;
    vendorHash = "sha256-EzJxvJilL+WqMim2g72ZktTOFifecTKppAOIE3P++FY=";
  };
  source = pkgs.lib.fileset.toSource {
    root = ../..;
    # Do not include the legacy tree or its secret manifests in the build.
    fileset = pkgs.lib.fileset.unions [
      ./.
      ../../clusters/blizzard-actual-pilot
    ];
  };
in
pkgs.runCommand "actual-pilot-manifests"
  {
    nativeBuildInputs = [
      python
      pkgs.kustomize
      pkgs.shellcheck
      pkgs.e2fsprogs
      pkgs.util-linux
      pkgs.systemd
      celCheck
    ];
  }
  ''
    cp -R ${source} work
    chmod -R u+w work
    cd work
    export PYTHONDONTWRITEBYTECODE=1
    export ACTUAL_PILOT_TEST_LIBREDIRECT=${pkgs.libredirect}/lib/libredirect.so
    python -m unittest discover -s tests/actual-pilot -v
    shellcheck clusters/blizzard-actual-pilot/actual/guest/bootstrap.sh
    touch "$out"
  ''
