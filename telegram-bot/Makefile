IAM_TOKEN := $$( yc iam create-token )

TF := tofu
TF_PLAN_FILE := .terraform/plan.bin
FUNC_FILES := f.py
FUNC_ARCHIVE := function.zip
TF_DIR := tf

test:
	$(TF) -chdir=$(TF_DIR) validate

plan:
	zip -j $(FUNC_ARCHIVE) $(FUNC_FILES)
	mv $(FUNC_ARCHIVE)  $(TF_DIR)
	$(TF) -chdir=$(TF_DIR) plan -var "auth_token=$(IAM_TOKEN)" -refresh=true -out "$(TF_PLAN_FILE)"
	
apply: 
	$(TF) -chdir=$(TF_DIR) apply "$(TF_PLAN_FILE)" 

clean:
	rm -rf $(TF_DIR)/$(FUNC_ARCHIVE) $(TF_DIR)/$(TF_PLAN_FILE)

destroy:
	$(TF) -chdir=$(TF_DIR) apply -var "auth_token=$(IAM_TOKEN)" -destroy -auto-approve

