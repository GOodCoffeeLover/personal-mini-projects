terraform {
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "= 0.119"
    }
  }
}

provider "yandex" {
  token     = var.auth_token
  folder_id = "b1g751c59739vmds469t"
}

