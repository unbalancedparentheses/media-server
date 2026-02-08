{
  description = "Claudia's nix-darwin media server configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nix-darwin = {
      url = "github:LnL7/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nix-darwin, home-manager }: {
    darwinConfigurations."claudia" = nix-darwin.lib.darwinSystem {
      modules = [
        ./configuration.nix
        ./services/media.nix
        ./services/proxy.nix
        ./services/backup.nix
        ./services/health.nix
        home-manager.darwinModules.home-manager
        ({ lib, ... }: {
          home-manager.useGlobalPkgs = true;
          home-manager.useUserPackages = true;
          home-manager.users.claudiabottasera = {
            imports = [ ./home.nix ];
            home.username = lib.mkForce "claudiabottasera";
            home.homeDirectory = lib.mkForce "/Users/claudiabottasera";
          };
        })
      ];
    };
  };
}
