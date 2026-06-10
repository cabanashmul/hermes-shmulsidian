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

      # NixOS module: adds the plugin to hermes-agent's extraPlugins,
      # provisions an isolated venv for sqlite-vec + fastembed (avoiding
      # collisions with Hermes's sealed nix venv), and patches the plugin
      # with a bootstrap that injects the venv at load time.
      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.hermes-agent;
          system = pkgs.stdenv.hostPlatform.system;
          pluginPkg = self.packages.${system}.default;

          # Paths on the HOST that map to /data/.hermes/ inside the container.
          hermesDir   = "${cfg.stateDir}/.hermes";
          venvDir     = "${hermesDir}/shmulsidian-venv";
          pluginsDir  = "${hermesDir}/plugins";
          localPlugin = "${pluginsDir}/shmulsidian-local";

          # Bootstrap snippet — written to a file, read by the patcher script.
          bootstrapFile = pkgs.writeText "shmulsidian-bootstrap.py" ''
            # ---- shmulsidian venv bootstrap (sqlite-vec + fastembed, isolated from Hermes) ----
            import ctypes as _ctypes
            import glob as _glob
            import sys as _sys
            _VENV_SITE = "${venvDir}/lib/python3.12/site-packages"
            _gcc_libs = sorted(_glob.glob("/nix/store/*-gcc-[0-9]*-lib/lib/libstdc++.so.6"))
            if _gcc_libs:
                try:
                    _ctypes.CDLL(_gcc_libs[-1], mode=_ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
            if _VENV_SITE not in _sys.path:
                _sys.path.insert(0, _VENV_SITE)
            # ---- end venv bootstrap ----
          '';

          # Script that provisions the isolated venv and patched plugin copy.
          # Runs on boot; idempotent — skips work if already done.
          setupScript = pkgs.writeShellScript "shmulsidian-semantic-setup" ''
            set -euo pipefail
            VENV="${venvDir}"
            LOCAL="${localPlugin}"
            STORE_PLUGIN="${pluginPkg}"
            BOOTSTRAP="${bootstrapFile}"

            # 1. Create venv + install packages (skip if already done)
            if [ ! -f "$VENV/bin/python3" ]; then
              echo "shmulsidian: creating isolated venv at $VENV"
              ${pkgs.uv}/bin/uv venv "$VENV" --python ${pkgs.python312}/bin/python3 2>/dev/null || \
              ${pkgs.uv}/bin/uv venv "$VENV" 2>/dev/null
              echo "shmulsidian: installing sqlite-vec + fastembed"
              ${pkgs.uv}/bin/uv pip install --python "$VENV/bin/python3" \
                sqlite-vec fastembed 2>/dev/null
            fi

            # 2. Copy plugin from nix store to writable location (skip if current)
            STORE_HASH=$(${pkgs.coreutils}/bin/md5sum "$STORE_PLUGIN/__init__.py" | cut -d' ' -f1)
            if [ -f "$LOCAL/__init__.py" ]; then
              LOCAL_HASH=$(${pkgs.coreutils}/bin/md5sum "$LOCAL/__init__.py" | cut -d' ' -f1)
              if grep -q "shmulsidian venv bootstrap" "$LOCAL/__init__.py" 2>/dev/null; then
                # Already patched — re-patch only if upstream source changed
                if [ "$LOCAL_HASH" != "$STORE_HASH" ]; then
                  NEED_UPDATE=1
                else
                  NEED_UPDATE=0
                fi
              else
                NEED_UPDATE=1
              fi
            else
              NEED_UPDATE=1
            fi

            if [ "$NEED_UPDATE" = "1" ]; then
              echo "shmulsidian: copying + patching plugin at $LOCAL"
              rm -rf "$LOCAL"
              cp -a "$STORE_PLUGIN" "$LOCAL"
              chmod -R u+w "$LOCAL"

              # 3. Patch __init__.py: insert bootstrap after the module docstring
              ${pkgs.python3}/bin/python3 -c "
            import re
            init_path = '$LOCAL/__init__.py'
            bootstrap_path = '$BOOTSTRAP'
            with open(init_path) as f:
                content = f.read()
            with open(bootstrap_path) as f:
                bootstrap = f.read()
            # Find end of first triple-quoted docstring
            m = re.match(r'(\"\"\".*?\"\"\")\s*', content, re.DOTALL)
            if m and 'shmulsidian venv bootstrap' not in content:
                pos = m.end()
                content = content[:pos] + '\n' + bootstrap + '\n' + content[pos:]
            with open(init_path, 'w') as f:
                f.write(content)
            "
            fi

            # 4. Create symlink (shmulsidian → shmulsidian-local)
            if [ -L "${pluginsDir}/shmulsidian" ]; then
              CURRENT=$(${pkgs.coreutils}/bin/readlink "${pluginsDir}/shmulsidian")
              if [ "$CURRENT" = "shmulsidian-local" ]; then
                echo "shmulsidian: symlink already correct"
                exit 0
              fi
            fi
            echo "shmulsidian: creating symlink shmulsidian -> shmulsidian-local"
            ln -sfn shmulsidian-local "${pluginsDir}/shmulsidian"
            chown -h ${cfg.user}:${cfg.group} "${pluginsDir}/shmulsidian"
          '';
        in {
          config = lib.mkIf cfg.enable {
            services.hermes-agent = {
              extraPlugins = [ pluginPkg ];
            };

            # Provision the isolated venv + patched plugin on boot.
            # Runs after tmpfiles (which creates the plugins dir) and
            # before the hermes-agent container starts.
            systemd.services.shmulsidian-semantic = {
              description = "Provision shmulsidian isolated venv and patched plugin";
              wantedBy = [ "hermes-agent.service" ];
              before = [ "hermes-agent.service" ];
              after = [ "systemd-tmpfiles-setup.service" ];
              serviceConfig = {
                Type = "oneshot";
                RemainAfterExit = true;
                ExecStart = setupScript;
              };
            };

            # Ensure the venv and plugin directories exist with correct ownership.
            systemd.tmpfiles.rules = [
              "d ${hermesDir}/shmulsidian-venv 0755 ${cfg.user} ${cfg.group} -"
              "d ${pluginsDir}/shmulsidian-local 0755 ${cfg.user} ${cfg.group} -"
            ];
          };
        };
    };
}
