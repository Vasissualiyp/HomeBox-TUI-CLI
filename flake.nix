{
  description = "HomeBox CLI/TUI dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;
        pythonEnv = python.withPackages (ps: with ps; [
          httpx
          click
          textual
          rich
        ]);
      in {
        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv ];
          shellHook = ''
            echo "HomeBox CLI/TUI dev shell ready"
            echo ""
            echo "Make sure EMAIL, PASSWORD, and URL are set:"
            echo "  source .env   (or: set -a; source .env; set +a)"
            echo ""
            echo "Usage:"
            echo "  python homebox_cli.py --help"
            echo "  python homebox_tui.py"
          '';
        };
      });
}
