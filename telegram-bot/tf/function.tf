resource "yandex_function" "echo-tg-func" {
  name               = "echo-tg-func"
  description        = "function for tg bot that repeats just what said"
  user_hash          = "tg-func-echo"
  runtime            = "python311"
  entrypoint         = "f.handler"
  memory             = "128"
  execution_timeout  = "10"
  service_account_id = var.sa_id
  tags               = ["tg-bot"]
  secrets {
    id                   = var.tg_lockbox_secret["id"]
    version_id           = var.tg_lockbox_secret["version_id"]
    key                  = "tg-bot-token"
    environment_variable = "TELEGRAM_BOT_TOKEN"
  }
  content {
    zip_filename = "function.zip"
  }
}

