{
  description = "F32 BMW iDrive 6 emulator with user-supplied PSdZData";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }:
    let
      # Apple silicon macOS is the verified release host. The Linux hosts
      # (including WSL2 on Windows) use ANGLE's Vulkan backend and SDL windows.
      systems = [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      qemuPatches = {
        omap5-core = ./nix/patches/qemu/0001-omap5-core.patch;
        sgx-diagnostics = ./nix/patches/qemu/0002-sgx-diagnostics.patch;
        usb-network = ./nix/patches/qemu/0003-usb-network.patch;
        graphics-mailbox = ./nix/patches/qemu/0004-graphics-mailbox.patch;
      };
      qemuPatchSeries = import ./nix/qemu-patches.nix { inherit qemuPatches; };
      perSystem = forAllSystems (system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ self.overlays.default ];
          };
          source = pkgs.lib.cleanSourceWith {
            src = ./.;
            filter = path: _type:
              let
                name = baseNameOf path;
                localData = [
                  ".git"
                  "build"
                  "captures"
                  "firmware"
                  "idrive6-cache"
                  "idrive6.toml"
                  "psdzdata"
                ];
              in !(builtins.elem name localData);
          };
          qemu = pkgs.qemu-omap5;
          shaderPython = pkgs.python3.withPackages (pythonPackages: [
            pythonPackages.capstone
            pythonPackages.pyyaml
          ]);
          # Match the pinned Vita3K translator's private builder API used by the
          # optional, host-side shader arithmetic diagnostics.
          glslangBuilder = pkgs.fetchFromGitHub {
            owner = "KhronosGroup";
            repo = "glslang";
            rev = "fc9889c889561c5882e83819dcaffef5ed45529b";
            hash = "sha256-HwFP4KJuA+BMQVvBWV0BCRj9U5I3CLEU+5bBtde2f6w=";
          };
          vitaShaderSource = pkgs.fetchFromGitHub {
            owner = "Vita3K";
            repo = "Vita3K";
            rev = "62727b58322ea7be10aaade575ac9ef82c58f5e8";
            hash = "sha256-iZoBhLMQy6k9u7ww7XPe3youFeWFUa3Wvv2gDjsobPo=";
          };
          angleServer = pkgs.stdenv.mkDerivation {
            pname = "idrive6-angle-rpc-server";
            version = "1";
            src = source;
            nativeBuildInputs = [ pkgs.clang ];
            buildInputs = [ pkgs.angle ];
            buildPhase = ''
              runHook preBuild
              clang++ -std=c++17 -O2 -Wall -Wextra -Werror \
                -I${pkgs.angle}/include -I$src/tools \
                $src/tools/angle-egl-rpc-server.cpp \
                -L${pkgs.angle}/lib -Wl,-rpath,${pkgs.angle}/lib -lEGL -lGLESv2 \
                -o angle-egl-rpc-server
              runHook postBuild
            '';
            installPhase = ''
              mkdir -p $out/bin
              install -m755 angle-egl-rpc-server $out/bin/angle-egl-rpc-server
            '';
          };
          # ANGLE's Vulkan backend needs a Vulkan driver. A Nix-built loader cannot
          # be relied on to find the host's GPU driver (and WSL2 has no conformant
          # one), so Linux defaults to Mesa's software lavapipe driver. Set
          # VK_DRIVER_FILES (or VK_ICD_FILENAMES) beforehand to use another driver.
          linuxVulkanSetup = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
            if [ -z "''${VK_DRIVER_FILES:-}" ] && [ -z "''${VK_ICD_FILENAMES:-}" ]; then
              export VK_DRIVER_FILES="${pkgs.mesa}/share/vulkan/icd.d/lvp_icd.${pkgs.stdenv.hostPlatform.parsed.cpu.name}.json"
            fi
          '';
          launcher = pkgs.writeShellApplication {
            name = "idrive6";
            runtimeInputs = [
              pkgs.python3
              pkgs.nix
              pkgs.coreutils
              pkgs.findutils
              pkgs.gnugrep
              pkgs.gawk
              pkgs.gnused
              pkgs.gnutar
              pkgs.gzip
              pkgs.bash
              pkgs.lzo
              pkgs.llvmPackages.lld
              pkgs.llvmPackages.llvm
              pkgs.clang
              qemu
              angleServer
            ];
            text = ''
              export IDRIVE_SOURCE_ROOT="$PWD"
              export IDRIVE_QEMU="${qemu}/bin/qemu-system-arm"
              export IDRIVE_KOMBI_QEMU="${pkgs.qemu-capricorn-m}/bin/qemu-system-mips64el"
              export IDRIVE_QNX_CLANG="${pkgs.llvmPackages.clang-unwrapped}/bin/clang"
              export IDRIVE_ANGLE_ROOT="${pkgs.angle}"
              export IDRIVE_LZO_LIBRARY="${pkgs.lzo}/lib/liblzo2${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}"
              export IDRIVE_HOST_CXX="${pkgs.clang}/bin/clang++"
              export IDRIVE_ANGLE_SERVER="${angleServer}/bin/angle-egl-rpc-server"
              export IDRIVE_NIX="${pkgs.nix}/bin/nix"
              ${linuxVulkanSetup}
              exec ${pkgs.python3}/bin/python3 ${source}/tools/idrive6-launch.py "$@"
            '';
          };
        in
        {
          packages = {
            default = launcher;
            idrive6 = launcher;
            qemu-omap5 = qemu;
            qemu-capricorn-m = pkgs.qemu-capricorn-m;
            angle-egl-rpc-server = angleServer;
          };

          apps.idrive6 = {
            type = "app";
            program = "${launcher}/bin/idrive6";
          };

          devShells = {
            default = pkgs.mkShell {
              packages = [
                qemu
                angleServer
                pkgs.angle
                pkgs.lzo
                pkgs.python3Packages.capstone
                pkgs.python3Packages.pyyaml
                pkgs.clang
                pkgs.llvmPackages.clang-unwrapped
                pkgs.llvmPackages.llvm
                pkgs.llvmPackages.lld
                pkgs.git
              ];
              IDRIVE_QEMU = "${qemu}/bin/qemu-system-arm";
              IDRIVE_ANGLE_SERVER = "${angleServer}/bin/angle-egl-rpc-server";
              IDRIVE_ANGLE_ROOT = "${pkgs.angle}";
              IDRIVE_HOST_CXX = "${pkgs.clang}/bin/clang++";
              IDRIVE_LZO_LIBRARY = "${pkgs.lzo}/lib/liblzo2${pkgs.stdenv.hostPlatform.extensions.sharedLibrary}";
              shellHook = linuxVulkanSetup;
            };

            shader = pkgs.mkShell {
              packages = with pkgs; [
                shaderPython
                clang
                lld
                cmake
                ninja
                spirv-tools
                spirv-cross
                vulkan-tools
              ];
              buildInputs = with pkgs; [ fmt vulkan-loader vulkan-headers ]
                ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.moltenvk ];
              IDRIVE_QNX_CLANG = "${pkgs.llvmPackages.clang-unwrapped}/bin/clang";
              IDRIVE_GLSLANG_INCLUDE = "${glslangBuilder}";
              IDRIVE_VITA3K_SOURCE = "${vitaShaderSource}";
              shellHook = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
                export VK_DRIVER_FILES="${pkgs.moltenvk}/share/vulkan/icd.d/MoltenVK_icd.json"
              '' + linuxVulkanSetup;
            };
          };
        });
    in
    {
      overlays.default = final: prev: {
        qemu-capricorn-m =
          assert prev.qemu.version == "11.1.2";
          (prev.qemu.override {
            # nixpkgs links qemu-kvm to the host architecture's emulator, so
            # that target must be built alongside the MIPS guest.
            hostCpuTargets = [ "mips64el-softmmu" "${prev.stdenv.hostPlatform.qemuArch}-softmmu" ];
            sdlSupport = true;
            openGLSupport = false;
          }).overrideAttrs (old: {
            patches = (old.patches or [ ]) ++ [
              ./nix/patches/capricorn-m/0001-add-capricorn-m-machine.patch
            ];
          });

        qemu-omap5 =
          assert prev.qemu.version == "11.1.2";
          (prev.qemu.override {
            # The app needs ARMv7, and nixpkgs links qemu-kvm to the host
            # architecture's emulator (aarch64 on Apple silicon, x86_64 on Intel).
            hostCpuTargets = [ "arm-softmmu" "${prev.stdenv.hostPlatform.qemuArch}-softmmu" ];
          }).overrideAttrs (old: {
            patches = (old.patches or [ ]) ++ qemuPatchSeries;
          });
      };

      packages = forAllSystems (system: perSystem.${system}.packages);
      apps = forAllSystems (system: perSystem.${system}.apps);
      devShells = forAllSystems (system: perSystem.${system}.devShells);
    };
}
