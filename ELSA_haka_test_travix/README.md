### This is a short-version of Tekton CI/CD pipeline running in minikube in local ###
----------------------------------------------

- A powerful tools, a based on Cloud-Native.
- Fully integrated with Google Cloud Service.
- A dev verision cover more than 85% production environment
- Pay as you go model interm of CI/CD


1. Init infra.

setup.sh
-----
- Install docker daemon
- Install minikube
- Install Tekton Operator
- Install Tekton Dashboard
- Install Tekton Pipeline
- Install Tekton Trigger
- config_env.yaml

2. Init Tekton Pipeline

- 01.pvc.yml
- 02.pipeline.yml
- 06.pipeline-run.yml
- SeviceAccount.yaml
- Role&RoleBinding
- Git secret
- Docker secret
- Nodejs config_map
- MongoDB config_map

3. Running Tekton CI/CD

build.sh & deploy.sh
----------------
- 03.task-git-clone.yaml
- 07.task-test-nodejs.yaml
- 09.task-build-push.yaml
- 05.task-auto-deploy-and-clean.yml
- 08.task-health-check.yaml
- 04.task-auto-rollback.yaml
- 00. trigger_event.yaml ( Ultilize, case by case)
- configmap_app.yaml
- configmap_mongodb.yaml

# Tekton Pipeline Example

This is a very simple but workable Tekton Pipeline development process that showcases:

- How to set up Tekton Pipeline, Dashboard from scratch;
- How to reuse the predefined `Task`s from [https://github.com/tektoncd/catalog](https://github.com/tektoncd/catalog);
- How to run through the `TaskRun`s step by step to walk through a typical Tekton pipeline development process;
- How to consolidate the desired `Task`s to make it a Tekton `Pipeline`;
- How to enable `Trigger`s for more advanced event-driven CI/CD.

As a result, we're going to have a very typical Tekton-powered pipeline which includes below steps:

1. `Git` clone the configured Git repository[here]https://github.com/traviixxx/testtekton, as the source;
2. Use `npm` to build the app;
3. Use `docker-dind` to build, by a given `Dockerfile`, and push the built image to target container registry;
4. Deploy it to Kubernetes.



## Prerequisites

Assuming you already have Tekton set up.

If not, you may simply run below file:

```sh
# Tekton Operator (full-component)
# For K8s

$ kubectl apply -f https://raw.githubusercontent.com/openshift/tektoncd-pipeline/release-v0.16.3/openshift/release/tektoncd-pipeline-v0.16.3.yaml


$ kubectl get pod -n tekton-pipelines
NAME                                           READY   STATUS    RESTARTS   AGE
tekton-dashboard-5675959458-xpj2q              1/1     Running   0          39s
tekton-pipelines-controller-58f8bb78fc-96zpz   1/1     Running   0          17m
tekton-pipelines-webhook-65bc86d45f-28wqt      1/1     Running   0          17m


\\------
Component   Org Version
Pipeline    tektoncd    v0.61.1
Dashboard   tektoncd    v0.48.0
Triggers    tektoncd    v0.28.0
Hub tektoncd    v1.17.0
Chains  tektoncd    v0.21.1
Results tektoncd    v0.11.0
Pipeline-as-code    openshift-pipelines v0.27.2
Manual Approval Gate    openshift-pipelines v0.2.2
```



## Create Dependent Resources

Let's create some dependant resources:

```sh
# A PVC to host shared data
kubectl apply -f pvc.yaml

# A ConfigMap to hold the App. manifest
kubectl apply -f configmap_app.yaml
kubectl apply -f configmap_mongodb.yaml

# A Role&RoleBinding for execute pipeline
kubectl apply -f role.yaml
kubectl apply -f rolebinding.yaml

# Create Github secret | Docker secret

-----> github

kubectl create secret generic github-secret \ 
--from-literal=username=traviixxx \ 
--from-literal=token=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 \

----> docker

kubectl create secret generic github-secret \ 
--from-literal=username=traviixxx \ 
--from-literal=password='ACB0**k1234' \


```

## Tasks


```sh
$ kubectl apply -f 03.task-git-clone.yml

$ kubectl apply -f 07.task-test-nodejs.yml

$ kubectl apply -f 09.task-build-push.yml

$ kubectl apply -f 05.task-auto-deploy-and-clean.yml

$ kubectl apply -f 08.task-health-check.yml

$ kubectl apply -f 04.task-auto-rollback.yml


```


## Walk it through by `TaskRun`s, step by step

TaskRuns are the way to **run** and **test** Tasks.

### 1. Git clone the repository

```sh
kubectl create -f ./03.task-git-clone.yml 
tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')
```

> Tip: a trick to always getting the last TR logs: `tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')`


### 2. A sample nodejs test

```sh
kubectl create -f ./07.task-test-nodejs.yml 
tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')
```


> OUTPT:
```
> hackathon-starter@8.0.1 test
> nyc mocha --timeout=60000 --exit



  User Model
    ✔ should create a new user
    ✔ should return error if user is not created
    ✔ should not create a user with the unique email
    ✔ should find user by email
    ✔ should remove user by email
    ✔ should check password (51ms)
    ✔ should generate gravatar without email and size
    ✔ should generate gravatar with size
    ✔ should generate gravatar with email


  9 passing (63ms)

----------|---------|----------|---------|---------|-------------------
File      | % Stmts | % Branch | % Funcs | % Lines | Uncovered Line #s 
----------|---------|----------|---------|---------|-------------------
All files |      68 |    66.66 |   66.66 |   70.83 |                   
 User.js  |      68 |    66.66 |   66.66 |   70.83 | 37-43,54          
----------|---------|----------|---------|---------|-------------------
npm notice
npm notice New minor version of npm available! 10.7.0 -> 10.8.3
npm notice Changelog: https://github.com/npm/cli/releases/tag/v10.8.3
npm notice To update run: npm install -g npm@10.8.3
npm notice

Step completed successfully
```

### 3. Build the source code by `npm` . 

```sh
kubectl create -f 09.task-build-push.yml
tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')
```


> OUTPT:
```
#7 [build 5/5] RUN npm install
#7 2.933 
#7 2.933 > hackathon-starter@8.0.1 postinstall
#7 2.933 > patch-package && npm run scss
#7 2.933 
#7 2.999 patch-package 8.0.0
#7 2.999 Applying patches...
#7 3.002 passport@0.6.0 ✔
#7 3.003 passport-oauth2@1.7.0 ✔
#7 3.065 
#7 3.065 > hackathon-starter@8.0.1 scss
#7 3.065 > sass --no-source-map --load-path=./ --update ./public/css:./public/css
#7 3.065 
#7 3.387 
#7 3.387 > hackathon-starter@8.0.1 prepare
#7 3.387 > if [ "$NODE_ENV" != "production" ]; then husky install; fi
#7 3.387 
#7 3.402 husky - git command not found, skipping install
#7 3.406 
#7 3.406 up to date, audited 807 packages in 2s
#7 3.406 
#7 3.406 162 packages are looking for funding
#7 3.406   run `npm fund` for details
#7 3.415 
#7 3.415 15 vulnerabilities (9 moderate, 6 high)
#7 3.415 
#7 3.415 To address all issues, run:
#7 3.415   npm audit fix
#7 3.415 
#7 3.415 Run `npm audit` for details.
#7 3.416 npm notice
#7 3.416 npm notice New patch version of npm available! 10.8.2 -> 10.8.3
#7 3.416 npm notice Changelog: https://github.com/npm/cli/releases/tag/v10.8.3
#7 3.416 npm notice To update run: npm install -g npm@10.8.3
#7 3.416 npm notice
#7 DONE 3.4s
```

----> Build the Docker image by `docker-dind`



> OUTPUT:

```
WARNING! Your password will be stored unencrypted in /root/.docker/config.json.
Configure a credential helper to remove this warning. See
https://docs.docker.com/engine/reference/commandline/login/#credentials-store

Login Succeeded
#1 [internal] load build definition from Dockerfile
#1 transferring dockerfile: 579B 0.0s done
#1 DONE 0.0s

#2 [internal] load metadata for docker.io/library/node:20-slim
#2 DONE 13.9s

#3 [internal] load .dockerignore
#3 transferring context: 2B done
#3 DONE 0.0s

#12 [build 1/5] FROM docker.io/library/node:20-slim@sha256:df85129996d6b7a4e...
#12 resolve docker.io/library/node:20-slim@sha256:df85129996d6b7a4ec702ebf2142cfa683f28b1d33446faec12168d122d3410d done
#12 sha256:2e87474dd18d69e2dd899cfa971645b13e4d3aca733fbf29b3593c201322bff0 1.93kB / 1.93kB done
#12 sha256:b7ade6730fabbf93524765fc5f5a919fdcfd6325afb09ee76bc91aa74230514b 6.86kB / 6.86kB done
#12 sha256:92c3b3500be621c72c7ac6432a9d8f731f145f4a1535361ffd3a304e55f7ccda 0B / 29.16MB 0.2s
#12 sha256:56acdc33ca7e9bd6dc8ad491706ab45fef59eda4960379a5689ca7b2f929e2b0 0B / 3
```
---> Push the Docker image



> OUTPUT:

```
WARNING! Your password will be stored unencrypted in /root/.docker/config.json.
Configure a credential helper to remove this warning. See
https://docs.docker.com/engine/reference/commandline/login/#credentials-store

Login Succeeded
The push refers to repository [docker.io/traviixxx/akatesthack]
10deb00b76fe: Preparing
6a0e33e71c2a: Preparing
0e390a37606c: Preparing
adf4563c8c66: Preparing
4df6602b1efc: Preparing
acc80c7cfda1: Preparing
59e359c8cc0f: Preparing
a4b2b831c466: Preparing
e644ff0c302d: Preparing
e644ff0c302d: Waiting
a4b2b831c466: Waiting
acc80c7cfda1: Waiting
59e359c8cc0f: Waiting
4df6602b1efc: Layer already exists
acc80c7cfda1: Layer already exists
59e359c8cc0f: Layer already exists
a4b2b831c466: Layer already exists
e644ff0c302d: Layer already exists
6a0e33e71c2a: Pushed
0e390a37606c: Pushed
adf4563c8c66: Pushed
10deb00b76fe: Pushed
COMMIT_SHA: digest: sha256:9086b6800a792f5addd09213b7b9a4d4cd87ce42a6dd21dc0d0230af67763e5c size: 2207

```

### 4. Deploy it

```sh
kubectl create -f 05.task-auto-deploy-and-clean.yml
tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')
```
--> Copy App. manifest


> INPUT:

```
computeResources: {}
image: bitnami/kubectl
name: copy-app-manifest
script: |
  #!/bin/sh
  set -e
  
  # Create the app.yaml and mongodb.yaml files from the ConfigMap
  kubectl get configmap my-web-app-manifest  -o jsonpath='{.data.app\.yaml}' > /workspace/source/app.yaml
  kubectl get configmap mongodb-manifest  -o jsonpath='{.data.mongodb\.yaml}' > /workspace/source/mongodb.yaml
  

```

--> Deploy


> OUTPUT:

```
NAME         READY   UP-TO-DATE   AVAILABLE   AGE
my-web-app   1/1     1            1           17h
Deployment my-web-app already exists.
Updating the deployment image...

  

```

-> Clean-UP


> OUTPUT:

```
computeResources: {}
image: alpine
name: clean-up
script: |
  #!/bin/sh
  set -e
  rm -rf /workspace/source/*
  
```

### 5. Service Health Check

--> check status


> OUTPUT:

```
Health check succeeded


  
```

```sh
kubectl create -f 08.task-health-check.yml
tkn tr logs -f -a $(tkn tr ls | awk 'NR==2{print $1}')
```

### 6. Auto Rollback

--> undo-if


> INPUT:

```
steps:
  - name: undo-if
  
```

## Set Up the Pipeline

Now that all the `Task`s and `TaskRun`s have been tested successfully, building a `Pipeline` is like playing with the Lego blocks.

So it's time to wrap everything into a Pipeline.

Please check the compiled pipeline out, from here [.../02.pipeline.yml](.../02.pipeline.yml).

And then install the `Pipeline`:

```sh

## Run the Pipeline

There are two major ways to run the pipeline manually, other than another popular automatic way by `Trigger`s:
- By creating `PipelineRun`
- By using Tekton CLI `tkn pipeline start`

So

```sh
kubectl create -f kubectl apply -f 02.pipeline.yml
tkn pr logs -f -a $(tkn pr ls | awk 'NR==2{print $1}')
```


## Tekton Dashboard

You may use `kubectl port-forward` to access the Tekton UI:

```
$ kubectl port-forward service/tekton-dashboard -n tekton-pipelines 9097:9097
Forwarding from 127.0.0.1:9097 -> 9097
Forwarding from [::1]:9097 -> 9097
```

And then open your browser and access: http://localhost:9097

![Tekton UI]


## 00.Triggers

Until now we have seen how to create `Task`s and `Pipeline`s and run them manually.

It's time to explore how to trigger it automatically using `Trigger`s, based on desired events.

### .Set Up Triggers in `tekton-pipelines` namespace

```sh
kubectl apply -f https://storage.googleapis.com/tekton-releases/triggers/previous/v0.11.2/release.yaml



### Typical Steps in `default` working namespace

```sh
# RBAC
kubectl apply -f Triggers/rbac.yaml -n tekton-demo

# Create the trigger template
kubectl apply -f Triggers/trigger-template.yaml -n tekton-demo

# Create the trigger binding
kubectl apply -f Triggers/trigger-binding.yaml -n tekton-demo

# Create the event listener
kubectl apply -f Triggers/event-listener.yaml -n tekton-demo

# Create Ing; or in OCP, expose the event listener svc as route
oc expose svc/el-tekton-event-listener -n tekton-demo
```

After this, we can get back the exposed endpoint and create a Webhook in GitHub Repo:
1. Within the repo, click **Settings** -> **Webhooks**;
2. Click **Add webhook** button at the right;
3. Fill up the form:
   - Payload URL: <The endpoint exposed for el-tekton-event-listener, e.g. route in OCP or ingress in K8s>
   - Content type: `application/json`
   - Secret: keep it blank;
   - Which events would you like to trigger this webhook? Keep the default `Just the push event`
   - Active: keep it checked
4. Click **Add webhook** to save it.

Now, once there is a push to the repo, it will trigger the `PipelineRun` to run through the `Pipeline`, which is quite cool.


## Clean Up

To clean up Resouce namespace:

```sh
# Instances
kubectl delete tr,pr,task,pipeline,pvc,route,svc,deployment --all 
kubectl delete tt,tb,el --all 

```

To clean up `tekton-*` namespace:

```sh
# Tekton Operator
kubectl delete -f tekton-operator-v072.yaml


```

