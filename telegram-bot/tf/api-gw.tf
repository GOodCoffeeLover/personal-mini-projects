resource "yandex_api_gateway" "tg-bot-gw" {
  name        = "tg-bot-gw"
  description = "gateway for calling functions from telegram bot"
  spec = templatefile("${path.module}/api-gw.yaml.tftpl", {
    function_id        = yandex_function.echo-tg-func.id
    function_name      = yandex_function.echo-tg-func.name
    service_account_id = var.sa_id
  })
}

