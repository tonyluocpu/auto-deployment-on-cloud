# 🤖 Autodeploy Chat System

A backend system powered by AI that automates the deployment of applications to the three cloud platforms in US: **AWS**, **Google Cloud Platform (GCP)**, and **Azure** — all from a natural language prompt and a GitHub repository URL.

The system utilizes **Large Language Models (LLMs)** to interpret deployment instructions and dynamically generate Terraform configurations and startup scripts for seamless provisioning and deployment.

---

## 🚀 Overview

The Autodeploy Chat System is a command-line tool that manages the full deployment lifecycle, from interpreting user intent to launching a live app:

1. **Intent Recognition**  
   An LLM interprets a natural language prompt to determine the target cloud provider and application type, but application type will be confirmed further upon scanning the repo. 

2. **Repository Analysis**  
   It scans the GitHub repository structure to identify key files such as `requirements.txt`, `Dockerfile`, or `app.py`.

3. **Artifact Generation**  
   Based on the analysis, it generates a `startup.sh` script and Terraform files customized for the selected cloud provider.

4. **Infrastructure Provisioning**  
   Terraform provisions a virtual machine (VM), along with networking and security resources.

5. **Application Deployment**  
   The VM executes the startup script to install dependencies, clone the repo, and launch the app—making it publicly accessible.

---

## ⚙️ Prerequisites

Ensure the following tools are installed and configured, a requirements.txt will be provided for all python libraries:

### 🔧 Tools

- **Python 3.x**
- **Git**
- **Terraform**
- **Docker** (optional but recommended for containerized apps)
- **Cloud CLI Tools**:
  - **AWS**: `aws`
  - **GCP**: `gcloud`
  - **Azure**: `az`

### 🔐 Required Variables

The system expects the following variables to be set in different places of the program:

| Variable                     | Description                                           |
|-----------------------------|-------------------------------------------------------|
| `OPENROUTER_API_KEY`        | API key for OpenRouter LLM access                     |
| `GCP_BILLING_ACCOUNT_ID`    | GCP Billing Account ID                                |
| `AWS_ACCESS_KEY_ID`         | AWS access key ID                                     |
| `AWS_SECRET_ACCESS_KEY`     | AWS secret key                                        |

**SSH Keys:**  
The Azure and AWS deployment logic attempts to locate an existing SSH public key from standard paths such as `~/.ssh/id_ed25519.pub` or `~/.ssh/id_rsa.pub`.  
This is a secure approach, as it avoids requiring private keys in the script or configuration.  
If no key is found, deployment may fail or prompt for one.

---

## 📁 Project Structure

```plaintext
auto-deployment-on-cloud/
├── tf_out_hello_world/ (names generated based on our example)
├── .gitignore
├── README.md
├── autodeploy_aws.py
├── autodeploy_chat_azure_gcp.py
|-- requirements.txt
```

---

## 🧑‍💻 Usage

Start the deployment process by running:

```bash
$ python autodeploy_aws.py
```

You will be prompted to enter a description and GitHub repo URL:

```
=== Autodeploy Chat System: Full Deployment Workflow ===

Describe your deployment (e.g., 'Deploy my Flask app on GCP'): Deploy my Flask app on GCP
GitHub repo URL [https://github.com/Arvo-AI/hello_world]: https://github.com/Arvo-AI/hello_world
```

Once complete, the application will be accessible via a public IP address.

---

### 🧹 To Destroy Resources

To clean up the deployed infrastructure:

```bash
$ cd tf_out_<repo_name>
$ terraform destroy -auto-approve
```

---

## 🛠️ Configuration Notes

> ⚠️ **Current Limitation**  
> Due to a known limitation, the system does not automatically detect API keys from `.env`.  
> To work around this, manually set the variables in your script before running it:

```python
os.environ['OPENROUTER_API_KEY'] = 'your_openrouter_api_key'  # https://openrouter.ai
os.environ['GCP_BILLING_ACCOUNT_ID'] = 'your_gcp_billing_account_id'  # Found in GCP Billing Dashboard, for creating new project if quota limit not reached
```


---

### 🔐 Cloud Provider Setup (MUST BE DONE PRIOR TO RUNNING THE PROGRAM)

#### GCP

```bash
gcloud auth application-default login
```

#### Azure

```bash
az login
```

Then confirm you're logged in with an active subscription. If you don't have an account, create one at: https://portal.azure.com/

#### AWS

Run the following and enter your credentials:

```bash
aws configure
```

You’ll be prompted for:

```
AWS Access Key ID: <your_access_key>
AWS Secret Access Key: <your_secret_key>
Default region name [us-east-1]: (press enter to accept default)
Default output format [json]: (press enter to accept default, but text is better)
```

---

## 🌟 Features

- Natural language → live cloud deployment
- Supports AWS, GCP, and Azure
- Automatic Terraform & startup script generation
- Multi-cloud support with minimal user input
- CLI-first interface

---

## 🧩 Dependencies

- Python 3.x
- Terraform
- Git
- Docker (optional)
- Cloud CLI tools (GCP, AWS, Azure)
- Access to an LLM API (e.g., OpenRouter)

---

## 📚 Documentation
- Inline comments in `autodeploy_aws.py`
- Generated Terraform files (`main.tf`, `startup.sh`, etc.)
---

## 🧪 Examples

### Deploy a Python Web App on GCP

```bash
$ python autodeploy_aws.py
# Describe your deployment: Deploy my Flask app on AWS
# GitHub repo URL: https://github.com/Arvo-AI/hello_world
```

### Faster Deployments (Azure or GCP)

AWS deployments may take longer due to policy and network setup. For faster deployments:

```bash
$ python autodeploy_chat_azure_gcp.py
# Describe your deployment: Deploy my Flask app on Azure
# GitHub repo URL: https://github.com/Arvo-AI/hello_world
```

---

## 🐛 Troubleshooting

- **Terraform errors**: Check your cloud CLI credentials and billing setup.
- **LLM fails to parse intent**: Be specific and include only one cloud provider (AWS, GCP, or Azure).
- **Missing dependencies**: Reinstall required packages and ensure CLI tools are in your system `PATH`.


