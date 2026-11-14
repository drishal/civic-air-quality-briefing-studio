{ pkgs, lib, config, inputs, ... }:

{
  packages = [ pkgs.git ];

  # numpy/pandas wheels need libz and libstdc++ at runtime on NixOS
  env.LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.zlib pkgs.stdenv.cc.cc.lib ];

  languages.python = {
    enable = true;
    package = pkgs.python312;
    uv = {
      enable = true;
      sync.enable = true;
    };
  };

  enterShell = ''
    python --version
  '';
}
