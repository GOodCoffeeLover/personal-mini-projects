IAM_TOKEN := $$( yc iam create-token )

TF := tofu
TF_PLAN_FILE := .terraform/plan.bin
FUNC_FILES := f.py
FUNC_ARCHIVE := function.zip
TF_DIR := tf
TELEGRAM_BOT_URL := "$$($(TF) -chdir=tf output -raw gateway_domain)/webhook"
TELEGRAM_BOT_TOKEN := "$$(yc lockbox payload get --name tg-bot-secrets --version-id e6q0eritjqmrf047007c --key tg-bot-token)"

test:
	$(TF) -chdir=$(TF_DIR) validate

plan:
	zip -j $(FUNC_ARCHIVE) $(FUNC_FILES)
	mv $(FUNC_ARCHIVE)  $(TF_DIR)
	$(TF) -chdir=$(TF_DIR) plan -var "auth_token=$(IAM_TOKEN)" -refresh=true -out "$(TF_PLAN_FILE)"
	
apply: 
	$(TF) -chdir=$(TF_DIR) apply "$(TF_PLAN_FILE)" 
	go run util.go -set -bot-token $(TELEGRAM_BOT_TOKEN) -bot-url $(TELEGRAM_BOT_URL)

clean:
	rm -rf $(TF_DIR)/$(FUNC_ARCHIVE) $(TF_DIR)/$(TF_PLAN_FILE)

destroy:
	go run util.go -del -bot-token $(TELEGRAM_BOT_TOKEN) -bot-url $(TELEGRAM_BOT_URL)
	$(TF) -chdir=$(TF_DIR) apply -var "auth_token=$(IAM_TOKEN)" -destroy -auto-approve

