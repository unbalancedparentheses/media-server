{
  description = "Claudia's nix-darwin media server configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nix-darwin = {
      url = "github:LnL7/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nix-darwin }: {
    darwinConfigurations."claudia" = nix-darwin.lib.darwinSystem {
      modules = [
        ./configuration.nix
        ./services/media.nix
      ];
    };
  };
}
