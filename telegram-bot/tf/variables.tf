variable "auth_token" {
  type = string
}

variable "tg_lockbox_secret" {
  default = {
    id         = "e6quh7vpu5kuik7u1q6p"
    version_id = "e6q0eritjqmrf047007c"
  }
  type = map(string)
}

variable "sa_id" {
  type    = string
  default = "ajebk41rmhbgsnd054k0"
}
