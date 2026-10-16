# Both images in one BuildKit run (docker buildx bake): built in parallel, one context upload.
#   sudo docker buildx bake                 # voice-agent-lab:next and voice-agent-lab-worker:next
#   TAG=latest sudo -E docker buildx bake   # other tag
# Used by deploy/deploy-all.sh on the VM and by .github/workflows/images.yml.
variable "TAG" { default = "next" }

group "default" { targets = ["app", "worker"] }

target "app" {
  context = "."
  tags    = ["voice-agent-lab:${TAG}"]
}

target "worker" {
  context = "."
  target  = "worker"
  tags    = ["voice-agent-lab-worker:${TAG}"]
}
