"""SSH Remote Workspace Bridge.

A thin remote filesystem + remote command execution layer over the system
OpenSSH client (SFTP subsystem for files, `ssh` for commands). The remote
host only needs a standard sshd with the sftp subsystem enabled - no agent
server, no Node runtime, no modern glibc.
"""

__version__ = "0.1.0"
