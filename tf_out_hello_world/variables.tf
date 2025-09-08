variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "location" {
  description = "Azure location/region"
  type        = string
  default     = "eastus"
}

variable "rg_name" {
  description = "Resource Group name"
  type        = string
  default     = "autodeploy-rg"
}

variable "vm_size" {
  description = "Azure VM size"
  type        = string
  default     = "Standard_B2s"
}

variable "admin_username" {
  description = "Admin username for the VM"
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key" {
  description = "SSH public key for the admin user"
  type        = string
}

variable "app_port" {
  description = "Application port"
  type        = number
}
