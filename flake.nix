{
  description = "hermes-shmulsidian — Obsidian vault memory provider for Hermes Agent";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in {
          # The plugin directory — contains plugin.yaml + __init__.py.
          # Hermes discovers this when placed in $HERMES_HOME/plugins/shmulsidian/
          # or passed via services.hermes-agent.extraPlugins.
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "hermes-shmulsidian";
            version = "0.1.0";
            src = ./plugin;
            dontBuild = true;
            installPhase = ''
              mkdir -p $out
              cp $src/plugin.yaml $out/
              cp $src/__init__.py $out/
            '';
          };
        }
      );

      # NixOS module: adds the plugin to hermes-agent's extraPlugins
      # and declares the Python dependencies it needs.
      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.hermes-agent;
          system = pkgs.stdenv.hostPlatform.system;
          pluginPkg = self.packages.${system}.default;
        in {
          config = lib.mkIf cfg.enable {
            services.hermes-agent = {
              extraPlugins = [ pluginPkg ];
              # NOTE: sqlite-vec and fastembed are NOT in extraPythonPackages
              # because fastembed pulls 'requests' which collides with hermes's
              # sealed venv. Install via pip in the container instead:
              #   pip install sqlite-vec fastembed
              # The plugin falls back to FTS5-only keyword search if these
              # are absent.
            };
          };
        };
    };
}
