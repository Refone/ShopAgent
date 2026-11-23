# 在 Github 上添加自动部署 Action

- GitHub 链接: https://github.com/Refone/ShopAgent
- Tencent 云服务器: `ubuntu@62.234.97.**`
- 部署分支: `main`
- 部署目录: `/home/ubuntu/deploy/ShopAgent`

## 1 准备材料

### 1.1 准备一对公私钥 🔒🔑

> 非对称加密密钥对中，
> 🔒 公钥作为**锁孔**，大家都知道锁的位置；
> 🔑 私钥作为**钥匙**，只有有钥匙的人才能打开锁，进门。

> 当前系统，GitHub Action 需要 ssh 到云服务器执行命令，
> 所以，
> ☁️ 云服务器是门，🔒 公钥放在云服务器上。
> 💟 GitHub 需要有钥匙，🔑 私钥放在 GitHub Secret 里面，

> 无所谓这对密钥在哪里生成，甚至在第三台机器（开发机）上生成都可以。
> 目标是铸成这对🔒锁和🔑钥匙，🔒随便给，但🔑要保管好。

#### 1.1.1 本地生成密钥对

```bash
ssh-keygen -t ed25519 -C "shopagent_deploy" -f "$HOME/.ssh/shopagent_deploy" -N ''
```

- `-t` 指定密钥类型为 **Ed25519**。
- `-C`\*为密钥添加**注释（comment）**，这个注释会附加在公钥文件的末尾。
- `-f` 指定**密钥文件的保存路径和文件名**。
- `-N` 设置**空密码短语（passphrase）**，防止自动化流程卡住。

执行后的结果，出现两个文件：

1. `~/.ssh/shopagent_deploy.pub` : 🔓 公钥

   <details>
   <summary> 文件内容 look like </summary>

   ```bash
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE9g/T4urdNLZ4DEK5vND5vhw0GbUKgJcKUIS0flkTrZ shopagent_deploy
   ```

   </details>

2. `~/.ssh/shopagent_deploy`：🔑 私钥

   <details>
   <summary>文件内容 look like</summary>

   ```bash
   -----BEGIN OPENSSH PRIVATE KEY-----
   b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
   ...
   w0GbUKgJcKUIS0flkTrZAAAAEHNob3BhZ2VudF9kZXBsb3kBAgMEBQ==
   -----END OPENSSH PRIVATE KEY-----
   ```

   </details>

#### 1.1.2 🎯 验证密钥对生成成功

```bash
ssh-keygen -y -f "$HOME/.ssh/shopagent_deploy" -P '' >/dev/null && echo "key ok"
```

验证完整性、且没有设置密码。期望输出：

```
key ok
```

### 1.2 准备 known host 🫆

> 虽然有了公钥和私钥，但是现在私钥方去访问公钥方，拿到的这个公钥，**有可能被人篡改过！！！**
> 所以会有一个交互，让我们确认，是否信任这个公钥，让我们输入"yes"（自己负责哦）。
> 然后，把这个身份信息，我们称之为 **主机指纹**，即，"主机名-公钥“ 映射，
> 存放在 `~/.ssh/known_hosts`中。
> 一旦在 known host 中，之后再 ssh 访问，就无脑相信了，也就不会再让我们 "yes"。

<details>
<summary>一个 known_hosts 文件内容 look like: </summary>

这个是 💻 本地开发机的 known_hosts

```
...
62.234.97.** ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHpipnbl2pr16r7oINxC9KiSC/rhZpTRIYUqe7KUxAib
...
```

- `62.234.97.**`是 ☁️ 云服务器主机 ip，有可能加盐，变成一串字符。
- `ssh-ed25519` 指定密钥对生成算法，同一个 IP 可能支持多种算法，有不只一行。
- `AAA...` 则是对应主机的公钥。它应该和 ☁️ 云服务器上 `cat /etc/ssh/ssh_host_ed25519_key.pub` 是完全一样的。

</details>

> 但是，我们不可能登录到 Github Runner 中，
> 在 Github Runner 里面预先 ssh 一次 ☁️，然后留下 known hosts。

> ⭐️ 幸运的是，known hosts 中关于 62.234.97.\*\* 的记录，
> 和世界上任何一台机器上 known hosts 中的记录应该完全一样（毕竟那三个信息都一致）。
> 所以，
> 我们在 💻 本地开发机上，ssh 一遍 ☁️ 云服务器，先获得 known host 记录，
> 然后通过 GitHub Secret 传给 GitHub Runner。

> 相当于，
> 我们提前替 GitHub Runner 去踩点

#### 1.2.1 替 GitHub 踩点

```bash
ssh-keyscan -p 22 -H -t ed25519 62.234.97.**
```

获得输出

```bash
# 62.234.97.**:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19
|1|UXrryd84YuE2ZPh4Iw4N0f0F36o=|TlJODmQYbwfLtmyKl9n0Ogjz+84= ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHpipnbl2pr16r7oINxC9KiSC/rhZpTRIYUqe7KUxAib
```

因为有 `-H` 参数，所以 ip 被“加盐”了，防逆向的安全措施。

#### 1.2.2 🎯 获得踩点 known host

这个输出可以存入一个文件，后面要用。

```bash
ssh-keyscan -p 22 -H -t ed25519 62.234.97.** > ~/Downloads/known_hosts.txt
```

### 1.3 准备云端 .env 文件 📜

这个好说，不讲究的化，项目里面开发用的 `.env` 文件就行

## 2 🔒 公钥 -> ☁️ 云服务器

### 2.1 上传公钥

```bash
ssh-copy-id -i "$HOME/.ssh/shopagent_deploy.pub" -p 22 ubuntu@62.234.97.**
```

<details>
<summary>期望交互：</summary>

```shell
/usr/bin/ssh-copy-id: INFO: Source of key(s) to be installed: "/Users/refone/.ssh/shopagent_deploy.pub"
/usr/bin/ssh-copy-id: INFO: attempting to log in with the new key(s), to filter out any that are already installed
/usr/bin/ssh-copy-id: INFO: 1 key(s) remain to be installed -- if you are prompted now it is to install the new keys
ubuntu@62.234.97.**'s password:

Number of key(s) added:        1

Now try logging into the machine, with: "ssh -i /Users/refone/.ssh/shopagent_deploy -p 22 'ubuntu@62.234.97.**'"
and check to make sure that only the key(s) you wanted were added.
```

</details>

### 2.2 🎯 验证上传成功

按照上一条命令的提示，用本地的 🔑 私钥，尝试免密访问 ☁️ 云服务器

```bash
ssh -i "$HOME/.ssh/shopagent_deploy" -o IdentitiesOnly=yes -p 22 ubuntu@62.234.97.** \
  'whoami; hostname; cd ~/deploy/ShopAgent && pwd'
```

<details>
<summary>期望交互：</summary>

```shell
ubuntu
VM-0-11-ubuntu
/home/ubuntu/deploy/ShopAgent
```

</details>

此时，☁️ 云服务器的 `.ssh/authorized_keys` 中应该会出现记录

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE9g/T4urdNLZ4DEK5vND5vhw0GbUKgJcKUIS0flkTrZ shopagent_deploy
```

也就是说，之后 GitHub Runner，持有这个公钥，并且自己身上带着对口的私钥，就可以 **免密登录** 这台服务器。

> TIps:
> 云服务器上还有一条记录，
>
> ```
> ssh-ed25519 AAAA...3ekrqi+ 710196926@qq.com
> ```
>
> 是的，这是我 💻 开发机的公钥，所以我可以 `ssh` 免密登录 ☁️ 云服务器

## 3 🔑 私钥 -> GitHub Secret

![](../assets/github-actions-new-secret.png)

_方案 A_：通过 GitHub -> Settings -> Secrets and variables -> Actions 添加新的 Secret，这里不多做演示了。

> GitHub Secret 由于安全机制，添加后的 Secrets 是加密的，无法再看到原值，只能替换，无法编辑，

👍🏻*方案 B* ：可以通过 `gh` 命令操作。在本地（有私钥的那台机器）

### 3.1 gh 上传私钥

`~/.ssh/shopagent_deploy` 是我们在第一步生成的私钥文件。

```shell
gh secret set DEPLOY_SSH_KEY -R Refone/ShopAgent < ~/.ssh/shopagent_deploy
```

### 3.2 🎯 验证 secret 更新成功

```
cd ~/Coding/ShopAgent
gh secret list
```

期望输出：

```
NAME             UPDATED
DEPLOY_SSH_KEY   about 1 minute ago
```

## 4. 其他 GitHub Secret 📝🫆📜

依旧是选择在 💻 开发机上执行 `gh` 命令

```bash
gh secret set DEPLOY_HOST -R Refone/ShopAgent --body '62.234.97.**'
gh secret set DEPLOY_USER -R Refone/ShopAgent --body 'ubuntu'
gh secret set DEPLOY_SSH_PORT -R Refone/ShopAgent --body '22'

# 设置云端项目路径
gh secret set DEPLOY_PATH -R Refone/ShopAgent --body '/home/ubuntu/deploy/ShopAgent'

# 第 1.2 准备的 known host
gh secret set DEPLOY_KNOWN_HOSTS -R Refone/ShopAgent < ~/Downloads/konwn_hosts.txt

# 第 1.2 准备的 .env 文件
gh secret set DEPLOY_ENV -R Refone/ShopAgent < ~/Coding/ShopAgent/.env
```

期望输出

```shell
✓ Set Actions secret DEPLOY_HOST for Refone/ShopAgent
✓ Set Actions secret DEPLOY_USER for Refone/ShopAgent
✓ Set Actions secret DEPLOY_SSH_PORT for Refone/ShopAgent
✓ Set Actions secret DEPLOY_PATH for Refone/ShopAgent
✓ Set Actions secret DEPLOY_KNOWN_HOSTS for Refone/ShopAgent
✓ Set Actions secret DEPLOY_ENV for Refone/ShopAgent
```

可用 `gh secret list` 检查

```bash
NAME                UPDATED
DEPLOY_ENV          about 1 minute ago
DEPLOY_HOST         about 1 minute ago
DEPLOY_KNOWN_HOSTS  about 1 minute ago
DEPLOY_PATH         about 1 minute ago
DEPLOY_SSH_KEY      about 10 minutes ago
DEPLOY_SSH_PORT     about 1 minute ago
DEPLOY_USER         about 1 minute ago
```

至此，GitHub Action 需要的配置就完成了。

## 5. GitHub workflow

GitHub 自动触发 CI 的关键，是项目中的 `.github/workflows` 文件夹中的工作流。

本项目自动部署的工作流为 `.github/workflows/deploy.yml`，由 AI 生成。
其中需要私下设置的变量，
例如：

```yml

...
jobs:
  deploy:
    name: Deploy to cloud server
    runs-on: ubuntu-latest
    timeout-minutes: 60

    defaults:
      run:
        shell: bash

    env:
      DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
      DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
      DEPLOY_SSH_PORT: ${{ secrets.DEPLOY_SSH_PORT }}
      DEPLOY_PATH_SECRET: ${{ secrets.DEPLOY_PATH }}
      DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
      DEPLOY_KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
      DEPLOY_ENV: ${{ secrets.DEPLOY_ENV }}
...
```

我们已经在前面通过 GitHub Secret 设置了。

现在，万事具备，两种方式触发 workflow：

1. Push 一下 main 分支
2. 在 GitHub Action 中主动触发 deploy 任务

应该就能触发 GitHub Action 的自动构建 + 部署了。

🏆
