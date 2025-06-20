import json
import logging
import base64
import re
import random
import string
import time
from datetime import datetime
from kubernetes import client
from utils.workspace_init import generate_init_script

logger = logging.getLogger(__name__)

# Initialize workspace_config with default values at the top of the file
workspace_config = {
    'github_branches': ['main'],
    'github_urls': []
}

class WorkspaceInitializer:
    """Service for initializing workspaces with common functionality for both regular workspaces and pool VMs"""
    
    def __init__(self, core_v1: client.CoreV1Api, apps_v1: client.AppsV1Api, workspace_domain: str, aws_account_id: str):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.workspace_domain = workspace_domain
        self.aws_account_id = aws_account_id

    def _generate_random_subdomain(self, length=8) -> str:
        """Generate a random subdomain name"""
        letters = string.ascii_lowercase + string.digits
        return ''.join(random.choice(letters) for i in range(length))

    def initialize_workspace(self, 
                           workspace_id: str, 
                           repo_name: str,
                           branch_name: str,
                           github_pat: str,
                           pool_name: str,
                           for_pool: bool = False,
                           build_timestamp: str = None) -> bool:
        """Initialize a workspace with all required resources"""
        try:
            if not build_timestamp:
                build_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

            # Generate unique subdomain and namespace for this workspace
            subdomain = self._generate_random_subdomain()
            namespace = f"workspace-{workspace_id}"
            fqdn = f"{subdomain}.{self.workspace_domain}"
            
            # Generate the password once here to use in multiple places
            password = self._generate_random_password()

            # Create workspace_ids dictionary that will be used throughout the initialization
            workspace_ids = {
                'namespace_name': namespace,
                'workspace_id': workspace_id,
                'build_timestamp': build_timestamp,
                'subdomain': subdomain,
                'fqdn': fqdn,
                'password': password  # Add password to workspace_ids
            }

            # Create workspace_config dictionary
            workspace_config = {
                'repo_name': repo_name,
                'github_urls': [f"https://github.com/{repo_name}"],
                'github_branches': [branch_name] if branch_name else ['main'],
                'pool_name': pool_name
            }
            
            logger.info(f"Initialized workspace_config: {workspace_config}")

            # Create namespace first
            self._create_namespace(workspace_ids, for_pool)

            # Create PVCs
            self._create_workspace_pvc(workspace_ids)
            self._create_registry_pvc(workspace_ids)

            # Create secrets and configmaps
            self._create_workspace_secret(workspace_ids, github_pat)  # Pass workspace_ids with password
            self._create_init_script_configmap(workspace_ids, workspace_config)
            self._create_feature_installation_configmap(workspace_ids)
            self._create_workspace_info_configmap(workspace_ids, workspace_config)

            # Copy necessary resources
            self._copy_port_detector_configmap({
                'namespace_name': namespace,
                'workspace_id': workspace_id,
            })
            self._copy_wildcard_certificate({
                'namespace_name': namespace,
                'workspace_id': workspace_id
            })
            self._create_registry_credentials({
                'namespace_name': namespace,
                'workspace_id': workspace_id
            })

            # Create core resources
            self._create_service_account({
                'namespace_name': namespace,
                'workspace_id': workspace_id
            })
            self._create_deployment({
                'namespace_name': namespace,
                'workspace_id': workspace_id,
                'build_timestamp': build_timestamp,
                'subdomain': subdomain
            }, {
                'repo_name': repo_name,
                'branch_name': branch_name,
                'pool_name': pool_name
            }, for_pool)

            self._create_service({
                'namespace_name': namespace,
                'workspace_id': workspace_id
            })
            self._create_ingress({
                'namespace_name': namespace,
                'workspace_id': workspace_id,
                'subdomain': subdomain
            })

            try:
                logger.info(f"Successfully initialized workspace in namespace: {namespace}")
            except Exception as e:
                logger.error(f"Error initializing workspace: {e}")
                return False

            return True
        except Exception as e:
            logger.error(f"Error initializing workspace: {e}")
            return False

    def _create_namespace(self, workspace_ids: dict, for_pool: bool = False):
        """Create Kubernetes namespace for the workspace"""
        namespace = client.V1Namespace(
            metadata=client.V1ObjectMeta(
                name=workspace_ids['namespace_name'],
                labels={
                    "workspace-id": workspace_ids['workspace_id'],
                    "app": "workspace",
                    "initialization": "in-progress",
                    "fqdn": workspace_ids['fqdn'],
                    "type": "pool" if for_pool else "workspace"
                }
            )
        )
        self.core_v1.create_namespace(namespace)
        logger.info(f"Created namespace: {workspace_ids['namespace_name']}")

    def _create_workspace_pvc(self, workspace_ids: dict):
        """Create PVC for workspace data"""
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name="workspace-data",
                namespace=workspace_ids['namespace_name']
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteMany"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": "10Gi"}
                ),
                storage_class_name="efs-sc"
            )
        )
        self.core_v1.create_namespaced_persistent_volume_claim(
            workspace_ids['namespace_name'], pvc)

    def _create_registry_pvc(self, workspace_ids: dict):
        """Create PVC for container registry storage"""
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name="registry-storage",
                namespace=workspace_ids['namespace_name']
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": "5Gi"}
                ),
                storage_class_name="efs-sc"
            )
        )
        self.core_v1.create_namespaced_persistent_volume_claim(
            workspace_ids['namespace_name'], pvc)

    def _create_workspace_secret(self, workspace_ids: dict, github_pat: str = None):
        """Create workspace secret with Github token and other credentials"""
        secret_data = {
            "password": workspace_ids['password']  # Use password from workspace_ids
        }
        if github_pat:
            secret_data["github_token"] = github_pat

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="workspace-secret",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            string_data=secret_data
        )
        self.core_v1.create_namespaced_secret(
            workspace_ids['namespace_name'], secret)

    def _create_init_script_configmap(self, workspace_ids: dict, workspace_config: dict):
        """Create ConfigMap containing workspace initialization script"""
        
        # First create the main initialization script
        init_script = generate_init_script(workspace_ids, workspace_config)

        # Add Docker configuration scripts
        wrapper_script = self._create_wrapper_dockerfile_script(workspace_ids, workspace_config)
        if wrapper_script:
            init_script += wrapper_script

        dockerfile_script = self._create_wrapper_dockerfile(workspace_ids)
        if dockerfile_script:
            init_script += dockerfile_script

        
        init_config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name="workspace-init",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data={"init.sh": init_script}
        )
        self.core_v1.create_namespaced_config_map(workspace_ids['namespace_name'], init_config_map)

    def _create_feature_installation_configmap(self, workspace_ids: dict):
        """Create ConfigMap for feature installation script"""
        feature_script = self._create_feature_installation_script()
        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name="feature-install",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data={"install-features.sh": feature_script}
        )
        self.core_v1.create_namespaced_config_map(
            workspace_ids['namespace_name'], config_map)

    def _create_workspace_info_configmap(self, workspace_ids: dict, workspace_config: dict):
        """Create ConfigMap with workspace information"""
        info = {
            "id": workspace_ids['workspace_id'],
            "repositories": workspace_config.get('github_urls', []),
            "branches": workspace_config.get('github_branches', []),
            "subdomain": workspace_ids.get('subdomain'),
            "fqdn": workspace_ids.get('fqdn'),
            "url": f"https://{workspace_ids.get('fqdn')}",
            "created": datetime.now().isoformat(),
            "pool_name": workspace_config.get('pool_name'),
            "password": workspace_ids.get('password'),
        }

        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name="workspace-info",
                namespace=workspace_ids['namespace_name'],
                labels={"app": "workspace"}
            ),
            data={"info": json.dumps(info)}
        )
        self.core_v1.create_namespaced_config_map(
            workspace_ids['namespace_name'], config_map)

    def _copy_port_detector_configmap(self, workspace_ids: dict):
        """Copy port-detector ConfigMap from workspace-system namespace"""
        try:
            port_detector = self.core_v1.read_namespaced_config_map(
                name="port-detector", 
                namespace="workspace-system"
            )
            
            new_config_map = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name="port-detector",
                    namespace=workspace_ids['namespace_name'],
                    labels={"app": "workspace"}
                ),
                data=port_detector.data
            )
            self.core_v1.create_namespaced_config_map(
                workspace_ids['namespace_name'], new_config_map)
        except Exception as e:
            logger.warning(f"Error copying port detector ConfigMap: {e}")

    def _copy_wildcard_certificate(self, workspace_ids: dict):
        """Copy wildcard certificate from workspace-system namespace"""
        try:
            cert = self.core_v1.read_namespaced_secret(
                name="workspace-domain-wildcard-tls", 
                namespace="workspace-system"
            )
            
            new_cert = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name="workspace-domain-wildcard-tls",
                    namespace=workspace_ids['namespace_name'],
                    labels={"app": "workspace"}
                ),
                data=cert.data,
                type=cert.type
            )
            self.core_v1.create_namespaced_secret(
                workspace_ids['namespace_name'], new_cert)
        except Exception as e:
            logger.warning(f"Error copying wildcard certificate: {e}")

    def _create_registry_credentials(self, workspace_ids: dict):
        """Create registry credentials secret"""
        auth_config = {
            "auths": {
                "registry.workspace-system.svc.cluster.local:5000": {
                    "auth": ""
                }
            }
        }
        auth_json = json.dumps(auth_config).encode()
        auth_b64 = base64.b64encode(auth_json).decode()

        registry_secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="registry-credentials",
                namespace=workspace_ids['namespace_name']
            ),
            type="kubernetes.io/dockerconfigjson",
            data={".dockerconfigjson": auth_b64}
        )
        self.core_v1.create_namespaced_secret(
            workspace_ids['namespace_name'], registry_secret)

    def _create_service_account(self, workspace_ids: dict):
        """Create service account for the workspace"""
        service_account = client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(
                name="workspace-controller",
                namespace=workspace_ids['namespace_name'],
                annotations={
                    "eks.amazonaws.com/role-arn": f"arn:aws:iam::{self.aws_account_id}:role/workspace-controller-role"
                }
            )
        )
        self.core_v1.create_namespaced_service_account(
            workspace_ids['namespace_name'], service_account)

    def _create_deployment(self, workspace_ids: dict, workspace_config: dict, for_pool: bool):
        """Create the workspace deployment"""
        init_containers = self._create_init_containers(workspace_ids, workspace_config, for_pool)
        volumes = self._create_volumes(workspace_ids)
        
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name="workspace",
                namespace=workspace_ids['namespace_name'],
                labels={
                    "app": "workspace",
                    "allowed-registry-access": "true"
                }
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={"app": "workspace"}
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app": "workspace",
                            "allowed-registry-access": "true"
                        }
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="workspace-controller",
                        init_containers=init_containers,
                        containers=[
                            self._create_code_server_container(workspace_ids, workspace_config),
                            self._create_port_detector_container()
                        ],
                        volumes=volumes,
                        image_pull_secrets=[
                            client.V1LocalObjectReference(name="registry-credentials")
                        ]
                    )
                )
            )
        )
        self.apps_v1.create_namespaced_deployment(
            workspace_ids['namespace_name'], deployment)

    def _create_service(self, workspace_ids: dict):
        """Create the workspace service"""
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name="workspace",
                namespace=workspace_ids['namespace_name']
            ),
            spec=client.V1ServiceSpec(
                ports=[
                    client.V1ServicePort(
                        port=8443,
                        target_port=8443,
                        name="workspace"
                    )
                ],
                selector={"app": "workspace"}
            )
        )
        self.core_v1.create_namespaced_service(
            workspace_ids['namespace_name'], service)

    def _create_ingress(self, workspace_ids: dict):
        """Create ingress for the workspace"""
        fqdn = workspace_ids.get('fqdn', f"{workspace_ids.get('subdomain')}.{self.workspace_domain}")
        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name="workspace",
                namespace=workspace_ids['namespace_name'],
                annotations={
                    "kubernetes.io/ingress.class": "nginx",
                    "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                    "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600"
                }
            ),
            spec=client.V1IngressSpec(
                tls=[
                    client.V1IngressTLS(
                        hosts=[f"*.{self.workspace_domain}"],
                        secret_name="workspace-domain-wildcard-tls"
                    )
                ],
                rules=[
                    client.V1IngressRule(
                        host=fqdn,
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path="/",
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name="workspace",
                                            port=client.V1ServiceBackendPort(
                                                number=8443
                                            )
                                        )
                                    )
                                )
                            ]
                        )
                    )
                ]
            )
        )
        networking_v1 = client.NetworkingV1Api()
        networking_v1.create_namespaced_ingress(
            workspace_ids['namespace_name'], ingress)

    def _cleanup_failed_workspace(self, workspace_ids: dict):
        """Clean up resources for a failed workspace initialization"""
        try:
            # Delete all deployments in the namespace first
            try:
                self.apps_v1.delete_collection_namespaced_deployment(
                    workspace_ids['namespace_name'],
                    propagation_policy="Foreground"
                )
            except Exception as e:
                logger.warning(f"Error deleting deployments during cleanup: {e}")

            # Delete all services
            try:
                self.core_v1.delete_collection_namespaced_service(
                    workspace_ids['namespace_name']
                )
            except Exception as e:
                logger.warning(f"Error deleting services during cleanup: {e}")

            # Delete all configmaps
            try:
                self.core_v1.delete_collection_namespaced_config_map(
                    workspace_ids['namespace_name']
                )
            except Exception as e:
                logger.warning(f"Error deleting configmaps during cleanup: {e}")

            # Delete all PVCs
            try:
                self.core_v1.delete_collection_namespaced_persistent_volume_claim(
                    workspace_ids['namespace_name']
                )
            except Exception as e:
                logger.warning(f"Error deleting PVCs during cleanup: {e}")

            # Finally delete the namespace
            self.core_v1.delete_namespace(workspace_ids['namespace_name'])
            
            # Wait for namespace deletion to complete
            max_retries = 30
            while max_retries > 0:
                try:
                    self.core_v1.read_namespace(workspace_ids['namespace_name'])
                    time.sleep(1)
                    max_retries -= 1
                except client.exceptions.ApiException as e:
                    if e.status == 404:  # Namespace is gone
                        break
            
            if max_retries == 0:
                logger.error(f"Timeout waiting for namespace {workspace_ids['namespace_name']} to be deleted")
            else:
                logger.info(f"Successfully cleaned up workspace: {workspace_ids['namespace_name']}")
                
        except Exception as e:
            logger.error(f"Error during workspace cleanup: {e}")
            # Don't raise, as this is called during error handling

    def _generate_random_password(self, length: int = 12) -> str:
        """Generate a random password"""
        import random
        import string
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    def _create_init_containers(self, workspace_ids: dict, workspace_config: dict, for_pool: bool) -> list:
        """Create initialization containers for the deployment"""
        containers = [
            self._create_workspace_init_container(),
            self._create_base_image_kaniko_container(workspace_ids),
            self._create_wrapper_kaniko_container(workspace_ids)
        ]
        return containers

    def _create_workspace_init_container(self) -> client.V1Container:
        """Create the main workspace initialization container"""
        return client.V1Container(
            name="init-workspace",
            image="buildpack-deps:22.04-scm",
            command=["/bin/bash", "/scripts/init.sh"],
            security_context=client.V1SecurityContext(
                capabilities=client.V1Capabilities(
                    add=["CHOWN", "FOWNER", "FSETID", "DAC_OVERRIDE"]
                )
            ),
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace-data",
                    mount_path="/workspaces",
                    sub_path="workspaces"
                ),
                client.V1VolumeMount(
                    name="init-script",
                    mount_path="/scripts"
                ),
                client.V1VolumeMount(
                    name="docker-sock",
                    mount_path="/var/run/docker.sock"
                )
            ],
            env=[
                client.V1EnvVar(
                    name="GITHUB_TOKEN",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name="workspace-secret",
                            key="github_token",
                            optional=True
                        )
                    )
                ),
                client.V1EnvVar(name="DOCKER_CONFIG", value="/kaniko/.docker/")
            ]
        )

    def _create_base_image_kaniko_container(self, workspace_ids: dict) -> client.V1Container:
        """Create Kaniko container for building base image"""
        return client.V1Container(
            name="build-base-image",
            image="gcr.io/kaniko-project/executor:latest",
            args=[
                "--dockerfile=/workspace/Dockerfile",
                "--context=/workspace",
                f"--destination={self.aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
                "--insecure",
                "--skip-tls-verify",
                "--verbosity=debug",
                "--push-retry=3",
                "--snapshotMode=time",  # More reliable snapshot mode
                "--use-new-run",  # New more stable run implementation
                "--cleanup"  # Clean up after build
            ],
            env=[
                client.V1EnvVar(name="DOCKER_CONFIG", value="/kaniko/.docker/"),
                client.V1EnvVar(name="HTTP_TIMEOUT", value="600s"),
                client.V1EnvVar(name="HTTPS_TIMEOUT", value="600s")
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace-data",
                    mount_path="/workspace",
                    sub_path="workspaces/.user-dockerfile"
                )
            ]
        )

    def _create_wrapper_kaniko_container(self, workspace_ids: dict) -> client.V1Container:
        """Create Kaniko container for building wrapper image"""
        return client.V1Container(
            name="build-wrapper-image",
            image="gcr.io/kaniko-project/executor:latest",
            args=[
                "--dockerfile=/workspace/Dockerfile",
                "--context=/workspace",
                f"--destination={self.aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
                "--insecure",
                "--skip-tls-verify",
                "--verbosity=debug",
                "--push-retry=3",
                "--snapshotMode=time",
                "--use-new-run",
                "--cleanup"
            ],
            env=[
                client.V1EnvVar(name="DOCKER_CONFIG", value="/kaniko/.docker/"),
                client.V1EnvVar(name="HTTP_TIMEOUT", value="600s"),
                client.V1EnvVar(name="HTTPS_TIMEOUT", value="600s"),
                client.V1EnvVar(name="BASE_IMAGE", value=f"{self.aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}")
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace-data",
                    mount_path="/workspace",
                    sub_path="workspaces/.code-server-wrapper"
                )
            ]
        )

    def _create_code_server_container(self, workspace_ids: dict, workspace_config: dict) -> client.V1Container:
        """Create the main code-server container"""
        return client.V1Container(
            name="code-server",
            image=f"{self.aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-wrapper-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}",
            image_pull_policy="Always",
            env=[
                client.V1EnvVar(name="PUID", value="1000"),
                client.V1EnvVar(name="PGID", value="1000"),
                client.V1EnvVar(name="TZ", value="UTC"),
                client.V1EnvVar(name="DEFAULT_WORKSPACE", value="/workspaces"),
                client.V1EnvVar(name="VSCODE_EXTENSIONS", value="/config/extensions"),
                client.V1EnvVar(name="CODE_SERVER_EXTENSIONS_DIR", value="/config/extensions"),
                client.V1EnvVar(name="VSCODE_USER_DATA_DIR", value="/config/data"),
                client.V1EnvVar(name="CS_DISABLE_GETTING_STARTED_OVERRIDE", value="true"),
                client.V1EnvVar(name="VSCODE_PROXY_URI", value=f"https://{workspace_ids['subdomain']}-{{{{port}}}}.{self.workspace_domain}/"),
                client.V1EnvVar(
                    name="PASSWORD",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name="workspace-secret",
                            key="password"
                        )
                    )
                ),
                client.V1EnvVar(name="DOCKER_HOST", value="unix:///var/run/docker.sock")
            ],
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace-data",
                    mount_path="/config",
                    sub_path="config"
                ),
                client.V1VolumeMount(
                    name="workspace-data",
                    mount_path="/workspaces",
                    sub_path="workspaces"
                ),
                client.V1VolumeMount(
                    name="docker-lib",
                    mount_path="/var/lib/docker"
                ),
                client.V1VolumeMount(
                    name="docker-sock",
                    mount_path="/var/run"
                )
            ],
            security_context=client.V1SecurityContext(
                privileged=True,
                capabilities=client.V1Capabilities(
                    add=["SYS_ADMIN", "NET_ADMIN"]
                )
            ),
            resources=client.V1ResourceRequirements(
                requests={
                    "cpu": "4",
                    "memory": "8Gi"
                },
                limits={
                    "cpu": "4", 
                    "memory": "8Gi"
                }
            )
        )

    def _create_port_detector_container(self) -> client.V1Container:
        """Create the port detector container"""
        return client.V1Container(
            name="port-detector",
            image="python:3.9-slim",
            command=["/bin/bash", "/scripts/port-detector.sh"],
            volume_mounts=[
                client.V1VolumeMount(
                    name="port-detector-script",
                    mount_path="/scripts",
                    read_only=True
                )
            ]
        )

    def _create_volumes(self, workspace_ids: dict) -> list:
        """Create volume definitions for the deployment"""
        return [
            client.V1Volume(
                name="workspace-data",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="workspace-data"
                )
            ),
            client.V1Volume(
                name="registry-storage",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="registry-storage"
                )
            ),
            client.V1Volume(
                name="init-script",
                config_map=client.V1ConfigMapVolumeSource(
                    name="workspace-init",
                    default_mode=0o755
                )
            ),
            client.V1Volume(
                name="code-server-data",
                empty_dir=client.V1EmptyDirVolumeSource()
            ),
            client.V1Volume(
                name="docker-lib",
                empty_dir=client.V1EmptyDirVolumeSource()
            ),
            client.V1Volume(
                name="docker-sock",
                empty_dir=client.V1EmptyDirVolumeSource()
            ),
            client.V1Volume(
                name="port-detector-script",
                config_map=client.V1ConfigMapVolumeSource(
                    name="port-detector",
                    default_mode=0o755
                )
            )
        ]

    def _create_feature_installation_script(self) -> str:
        """Create the feature installation script"""
        # This will use the existing feature installation script logic
        # from pool_service.py
        return """#!/bin/bash
# Script to install common dev container features
set -e

FEATURES_FILE="/workspaces/.devcontainer-features"
if [ ! -f "$FEATURES_FILE" ]; then
    echo "No features file found, skipping feature installation"
    exit 0
fi

echo "Installing dev container features from features file"
echo "Features content:"
cat "$FEATURES_FILE"

# Convert features file to JSON for easier parsing
FEATURES_JSON=$(cat "$FEATURES_FILE")

# Helper function to check if a feature exists
feature_exists() {
    echo "$FEATURES_JSON" | grep -q "\"$1\""
}

# Helper to extract feature version/options
get_feature_option() {
    local feature=$1
    local option=$2
    local default=$3
    
    # Try to extract the version or option using grep and sed
    # Format is typically "feature": { "version": "value", "optionName": "value" }
    result=$(echo "$FEATURES_JSON" | grep -o "\"$feature\"[^}]*" | grep -o "\"$option\"[^,}]*" | grep -o "\"[^\"]*\"$" | tr -d '"' || echo "")
    
    if [ -z "$result" ]; then
        echo "$default"
    else
        echo "$result"
    fi
}

# Install Docker feature
if feature_exists "docker"; then
    echo "Installing Docker feature"
    # Docker is already installed by the post-start script
    echo "✓ Docker already configured"
fi

# Install Docker-in-Docker feature (alternative to Docker)
if feature_exists "docker-in-docker" || feature_exists "docker-from-docker"; then
    echo "Installing Docker-in-Docker feature"
    # Docker is already installed by the post-start script
    echo "✓ Docker already configured via post-start script"
    
    # Add Docker Compose v2 if not already added
    if ! command -v docker-compose &> /dev/null; then
        echo "Installing Docker Compose v2"
        mkdir -p /usr/local/lib/docker/cli-plugins
        curl -SL "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-$(uname -m)" -o /usr/local/lib/docker/cli-plugins/docker-compose
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        ln -sf /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose
        echo "✓ Docker Compose installed: $(docker-compose version)"
    fi
fi

# Install Node.js feature
if feature_exists "node"; then
    echo "Installing Node.js feature"
    VERSION=$(get_feature_option "node" "version" "lts")
    
    # Install Node.js using NVM
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.3/install.sh | bash
    export NVM_DIR="$HOME/.nvm"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
    
    if [ "$VERSION" = "lts" ] || [ "$VERSION" = "latest" ]; then
        nvm install --lts
    else
        nvm install "$VERSION"
    fi
    # Add NVM to shell initialization
    echo 'export NVM_DIR="$HOME/.nvm"' >> /etc/profile.d/nvm.sh
    echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"' >> /etc/profile.d/nvm.sh
    
    echo "✓ Node.js $(node -v) installed"
fi

# Install Python feature
if feature_exists "python"; then
    echo "Installing Python feature"
    VERSION=$(get_feature_option "python" "version" "3.10")
    INSTALL_TOOLS=$(get_feature_option "python" "installTools" "true")
    INSTALL_JUPYTER=$(get_feature_option "python" "installJupyter" "false")
    
    # Install Python with apt
    apt-get update
    apt-get install -y python3 python3-venv python3-pip
    
    # Create symbolic links
    ln -sf /usr/bin/python3 /usr/bin/python
    
    echo "✓ Python $(python3 --version) installed"
    
    # Install common tools if requested
    if [ "$INSTALL_TOOLS" = "true" ]; then
        echo "Installing Python tools"
        pip3 install --no-cache-dir ipython pytest pylint flake8 black
        echo "✓ Common Python tools installed"
    fi
    
    # Install Jupyter if requested
    if [ "$INSTALL_JUPYTER" = "true" ]; then
        echo "Installing Jupyter"
        pip3 install --no-cache-dir jupyter notebook
        echo "✓ Jupyter installed"
    fi
fi

# Install Go feature
if feature_exists "go"; then
    echo "Installing Go feature"
    VERSION=$(get_feature_option "go" "version" "latest")
    
    if [ "$VERSION" = "latest" ]; then
        VERSION=$(curl -s https://go.dev/VERSION?m=text | head -n1)
    fi
    
    # Download and install Go
    curl -sSL "https://golang.org/dl/${VERSION}.linux-amd64.tar.gz" -o go.tar.gz
    tar -C /usr/local -xzf go.tar.gz
    rm go.tar.gz
    
    # Add Go to PATH
    echo 'export PATH=$PATH:/usr/local/go/bin' > /etc/profile.d/go.sh
    echo 'export PATH=$PATH:$HOME/go/bin' >> /etc/profile.d/go.sh
    
    # Set up environment for current session
    export PATH=$PATH:/usr/local/go/bin
    
    echo "✓ Go $(go version) installed"
fi

# Install Java feature
if feature_exists "java"; then
    echo "Installing Java feature"
    VERSION=$(get_feature_option "java" "version" "17")
    
    # Install OpenJDK
    apt-get update
    apt-get install -y openjdk-${VERSION}-jdk
    
    echo "✓ Java $(java -version 2>&1 | head -n 1) installed"
fi

# Install Rust feature
if feature_exists "rust"; then
    echo "Installing Rust feature"
    
    # Install Rust using rustup
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    
    # Add Rust to PATH
    echo 'export PATH=$PATH:$HOME/.cargo/bin' > /etc/profile.d/rust.sh
    
    # Set up environment for current session
    export PATH=$PATH:$HOME/.cargo/bin
    
    echo "✓ Rust $(rustc --version) installed"
fi

# Install .NET feature
if feature_exists "dotnet"; then
    echo "Installing .NET feature"
    VERSION=$(get_feature_option "dotnet" "version" "latest")
    
    # Install .NET SDK
    apt-get update
    apt-get install -y wget
    
    if [ "$VERSION" = "latest" ]; then
        wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh
        chmod +x dotnet-install.sh
        ./dotnet-install.sh
    else
        wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh
        chmod +x dotnet-install.sh
        ./dotnet-install.sh --version $VERSION
    fi
    
    # Add .NET to PATH
    echo 'export PATH=$PATH:$HOME/.dotnet' > /etc/profile.d/dotnet.sh
    
    # Set up environment for current session
    export PATH=$PATH:$HOME/.dotnet
    
    echo "✓ .NET installed"
fi

# Install PHP feature
if feature_exists "php"; then
    echo "Installing PHP feature"
    VERSION=$(get_feature_option "php" "version" "8.2")
    COMPOSER=$(get_feature_option "php" "composer" "true")
    
    # Install PHP
    apt-get update
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:ondrej/php
    apt-get update
    apt-get install -y php${VERSION} php${VERSION}-cli php${VERSION}-common php${VERSION}-curl php${VERSION}-mbstring php${VERSION}-mysql php${VERSION}-xml php${VERSION}-zip
    
    # Install Composer if requested
    if [ "$COMPOSER" = "true" ]; then
        echo "Installing Composer"
        curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer
        echo "✓ Composer installed: $(composer --version)"
    fi
    
    echo "✓ PHP installed: $(php -v | head -n 1)"
fi

# Install common utilities
if feature_exists "common-utils"; then
    echo "Installing common utilities"
    
    apt-get update
    apt-get install -y wget curl vim git jq unzip zip sudo 
    apt-get install -y build-essential pkg-config libssl-dev
    
    echo "✓ Common utilities installed"
fi

# Install GitHub CLI
if feature_exists "github-cli"; then
    echo "Installing GitHub CLI"
    
    # Install GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update
    apt-get install -y gh
    
    echo "✓ GitHub CLI $(gh --version | head -n 1) installed"
fi

# Install Azure CLI
if feature_exists "azure-cli"; then
    echo "Installing Azure CLI"
    
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash
    
    echo "✓ Azure CLI $(az --version | head -n 1) installed"
fi

# Install AWS CLI
if feature_exists "aws-cli"; then
    echo "Installing AWS CLI"
    
    apt-get update
    apt-get install -y unzip
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
    unzip awscliv2.zip
    ./aws/install
    rm -rf aws awscliv2.zip
    
    echo "✓ AWS CLI $(aws --version) installed"
fi

# Install Terraform
if feature_exists "terraform"; then
    echo "Installing Terraform"
    VERSION=$(get_feature_option "terraform" "version" "latest")
    
    apt-get update
    apt-get install -y gnupg software-properties-common curl
    
    curl -fsSL https://apt.releases.hashicorp.com/gpg | apt-key add -
    apt-add-repository "deb [arch=amd64] https://apt.releases.hashicorp.com $(lsb_release -cs) main"
    apt-get update
    
    if [ "$VERSION" = "latest" ]; then
        apt-get install -y terraform
    else
        apt-get install -y terraform=$VERSION
    fi
    
    echo "✓ Terraform $(terraform version | head -n 1) installed"
fi

# Install kubectl
if feature_exists "kubectl" || feature_exists "kubernetes-tools"; then
    echo "Installing kubectl"
    VERSION=$(get_feature_option "kubectl" "version" "latest")
    
    if [ "$VERSION" = "latest" ]; then
        VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)
    fi
    
    curl -LO "https://dl.k8s.io/release/$VERSION/bin/linux/amd64/kubectl"
    chmod +x kubectl
    mv kubectl /usr/local/bin/
    
    echo "✓ kubectl $(kubectl version --client -o json | jq -r '.clientVersion.gitVersion') installed"
fi

echo "Feature installation completed"
"""

    def _create_wrapper_dockerfile_script(self, workspace_ids: dict, workspace_config: dict) -> str:
        """Generate script to create a wrapper Dockerfile using the user's Dockerfile as a base."""
        repo_name = workspace_config['repo_name']
        init_script = f"""
        # Create directory for wrapper Dockerfile and user Dockerfile
        mkdir -p /workspaces/.code-server-wrapper
        mkdir -p /workspaces/.user-dockerfile
        cd /workspaces/.code-server-wrapper

        # Locate the user's Dockerfile in their repo
        USER_REPO_PATH="/workspaces/{repo_name}"
        DOCKERFILE_PATH="$USER_REPO_PATH/.devcontainer/Dockerfile"
        DEVCONTAINER_JSON_PATH="$USER_REPO_PATH/.devcontainer/devcontainer.json"

        # Debugging and validation
        echo "DEBUG: Checking repository and Dockerfile"
        if [ -d "$USER_REPO_PATH" ]; then
            echo "DEBUG: Repository directory exists at $USER_REPO_PATH"
            ls -la "$USER_REPO_PATH"
        else
            echo "DEBUG: ERROR - Repository directory does not exist at $USER_REPO_PATH"
        fi

        if [ -d "$USER_REPO_PATH/.devcontainer" ]; then
            echo "DEBUG: .devcontainer directory exists"
            ls -la "$USER_REPO_PATH/.devcontainer"
        else
            echo "DEBUG: .devcontainer directory does not exist"
        fi

        if [ -f "$DOCKERFILE_PATH" ]; then
            echo "DEBUG: Dockerfile exists at $DOCKERFILE_PATH"
            cat "$DOCKERFILE_PATH" | head -n 10
        else
            echo "DEBUG: Dockerfile does not exist at $DOCKERFILE_PATH"
        fi

        if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
            echo "DEBUG: devcontainer.json exists at $DEVCONTAINER_JSON_PATH"
            cat "$DEVCONTAINER_JSON_PATH" | head -n 20
        else
            echo "DEBUG: devcontainer.json does not exist at $DEVCONTAINER_JSON_PATH"
        fi

        # Clone repository if needed
        if [ ! -d "$USER_REPO_PATH" ]; then
            echo "Repository not found at $USER_REPO_PATH, attempting to clone again"
            cd /workspaces

            BRANCH="{workspace_config['github_branches'][0] if workspace_config['github_branches'] and workspace_config['github_branches'][0] else ''}"

            if [ ! -z "$BRANCH" ]; then
                echo "Cloning with specific branch: $BRANCH"
                git clone -b $BRANCH {workspace_config['github_urls'][0]} {repo_name}
            else
                echo "Cloning with default branch"
                git clone {workspace_config['github_urls'][0]} {repo_name}
            fi

            git config --global --add safe.directory /workspaces/{repo_name}
        fi

        # Check again after potential re-cloning
        if [ -f "$DOCKERFILE_PATH" ]; then
            echo "Found user Dockerfile at $DOCKERFILE_PATH"
            cp "$DOCKERFILE_PATH" /workspaces/.user-dockerfile/Dockerfile

            if [ -f "$DEVCONTAINER_JSON_PATH" ]; then
                echo "Found devcontainer.json - processing configuration"
                cp "$DEVCONTAINER_JSON_PATH" /workspaces/.user-dockerfile/

                if ! command -v jq &> /dev/null; then
                    echo "Installing jq to parse devcontainer.json"
                    apt-get update && apt-get install -y jq tmux
                fi

                EXTENSIONS=$(jq -r '.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ -z "$EXTENSIONS" ]; then
                    EXTENSIONS=$(jq -r '.customizations.vscode.extensions[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                fi

                if [ ! -z "$EXTENSIONS" ]; then
                    echo "$EXTENSIONS" > /workspaces/.extensions-list
                fi

                SETTINGS=$(jq -r '.settings // .customizations.vscode.settings // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$SETTINGS" ]; then
                    mkdir -p /workspaces/.vscode
                    echo "$SETTINGS" > /workspaces/.vscode/settings.json
                fi

                FEATURES=$(jq -r '.features // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$FEATURES" ]; then
                    echo "$FEATURES" > /workspaces/.devcontainer-features
                fi

                PORTS=$(jq -r '.forwardPorts[]? // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$PORTS" ]; then
                    echo "$PORTS" > /workspaces/.forward-ports
                fi

                CUSTOMIZATIONS=$(jq -r '.customizations // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$CUSTOMIZATIONS" ]; then
                    echo "$CUSTOMIZATIONS" > /workspaces/.customizations
                fi

                ENV_VARS=$(jq -r '.containerEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$ENV_VARS" ]; then
                    echo "$ENV_VARS" > /workspaces/.container-env
                fi

                REMOTE_ENV_VARS=$(jq -r '.remoteEnv // empty | to_entries[] | "\(.key)=\(.value)"' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$REMOTE_ENV_VARS" ]; then
                    echo "$REMOTE_ENV_VARS" > /workspaces/.remote-env
                fi

                REMOTE_USER=$(jq -r '.remoteUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                CONTAINER_USER=$(jq -r '.containerUser // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)

                if [ ! -z "$REMOTE_USER" ]; then
                    echo "REMOTE_USER=$REMOTE_USER" > /workspaces/.user-config
                fi

                if [ ! -z "$CONTAINER_USER" ]; then
                    echo "CONTAINER_USER=$CONTAINER_USER" >> /workspaces/.user-config
                fi

                POST_CREATE_CMD=$(jq -r '.postCreateCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$POST_CREATE_CMD" ]; then
                    echo "$POST_CREATE_CMD" > /workspaces/post-create-command.sh
                    chmod +x /workspaces/post-create-command.sh
                fi

                POST_START_CMD=$(jq -r '.postStartCommand // empty' "$DEVCONTAINER_JSON_PATH" 2>/dev/null)
                if [ ! -z "$POST_START_CMD" ]; then
                    echo "$POST_START_CMD" > /workspaces/post-start-command.sh
                    chmod +x /workspaces/post-start-command.sh
                fi
            fi
        else
            echo "Warning: No Dockerfile found at $DOCKERFILE_PATH"
            echo "Using default linuxserver/code-server image instead"
            echo "FROM linuxserver/code-server:latest" > /workspaces/.user-dockerfile/Dockerfile
        fi
        """

    def _create_wrapper_dockerfile(self, workspace_ids: dict) -> str:
        """Create a wrapper Dockerfile using the user's image as a base."""
        return f"""cat > Dockerfile << 'EOF'
FROM {self.aws_account_id}.dkr.ecr.us-east-1.amazonaws.com/workspace-images:custom-user-{workspace_ids['namespace_name']}-{workspace_ids['build_timestamp']}

RUN git config --global --add safe.directory /workspaces && \\
    git config --global --add safe.directory '*'

RUN apt-get update && apt-get install -y \\
    curl \\
    git \\
    gnupg2 \\
    jq \\
    procps \\
    lsb-release \\
    sudo \\
    tmux \\
    vim \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://code-server.dev/install.sh | sh

EXPOSE 8444

ENTRYPOINT ["/bin/bash", "-c", "if [ -f /workspaces/install-features.sh ]; then /workspaces/install-features.sh; fi && if [ -f /workspaces/setup-env.sh ]; then source /workspaces/setup-env.sh; fi && if [ -f /workspaces/install-extensions.sh ]; then /workspaces/install-extensions.sh; fi && if [ -f /workspaces/run-lifecycle.sh ]; then /workspaces/run-lifecycle.sh & fi && /usr/bin/code-server --bind-addr 0.0.0.0:8444 --auth password --user-data-dir /config/data --extensions-dir /config/extensions /workspaces"]
EOF"""