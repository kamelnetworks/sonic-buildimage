/***************************************************************************
 *   Copyright (C) 2021 Celestica  Corp                                    *
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 *   This program is distributed in the hope that it will be useful,       *
 *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
 *   GNU General Public License for more details.                          *
 *                                                                         *
 *   You should have received a copy of the GNU General Public License     *
 *   along with this program; if not, write to the                         *
 *   Free Software Foundation, Inc.,                                       *
 *   59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.             *
 ***************************************************************************/

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/err.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/notifier.h>
#include <linux/reboot.h>
#include <linux/uaccess.h>
#include <linux/watchdog.h>

#define REBOOT_CAUSE_REG   0xA106
#define WDT_TIMER_REG      0xA181
#define WDT_ENABLE_REG     0xA182
#define WDT_FEED_REG       0xA183
#define WDT_PUNCH_REG      0xA184
#define WDT_START_FEED     0x01
#define WDT_STOP_FEED      0x00

#define MAX_TIMER_VALUE     0x07    /* 0-200ms 1-30s 2-1min 3-3min 4-4min 5-5min 6-7min 7-10min */
#define DEFUALT_TIMER_VALUE 0x02    /* 2-1min */
#define WDT_ENABLE          0x01
#define WDT_DISABLE         0x00
#define WDT_RESTART         0x00
#define DRVNAME "basecpld_wdt"

static const int max_timeout = MAX_TIMER_VALUE;

static int timeout = DEFUALT_TIMER_VALUE;	/* default 1min */
module_param(timeout, int, 0);
MODULE_PARM_DESC(timeout, "Start watchdog timer on module load with"
	" given initial timeout, range 0x00~0x07(0-200ms 1-30s 2-1min 3-3min 4-4min 5-5min 6-7min 7-10min)."
	" Zero (default) disables this feature.");

static bool nowayout = WATCHDOG_NOWAYOUT;
module_param(nowayout, bool, 0444);
MODULE_PARM_DESC(nowayout, "Disable watchdog shutdown on close");

struct watchdog_data {
	unsigned long	opened;
	struct mutex	lock;
	char		expect_close;
	struct watchdog_info ident;

	unsigned short	timeout;
	u8		timer_val;	/* content for the wd_time register */
	char		caused_reboot;	/* last reboot was by the watchdog */
};

static struct watchdog_data watchdog = {
	.lock = __MUTEX_INITIALIZER(watchdog.lock),
};

static int watchdog_set_timeout(int timeout)
{
	if (timeout <= 0
	 || timeout >  max_timeout) {
		pr_err("watchdog timeout out of range\n");
		return -EINVAL;
	}

	mutex_lock(&watchdog.lock);

	watchdog.timeout = timeout;
	if (timeout > MAX_TIMER_VALUE) {
		watchdog.timer_val = MAX_TIMER_VALUE;
	} else {
		watchdog.timer_val = timeout;
	}
	/* Set timer value */
	outb(watchdog.timer_val, WDT_TIMER_REG);
	mutex_unlock(&watchdog.lock);

	return 0;
}

static void watchdog_keepalive(void)
{

	mutex_lock(&watchdog.lock);
	
	/* start feed watchdog */
	outb(WDT_START_FEED, WDT_FEED_REG);
	/* stop feed watchdog */
	outb(WDT_STOP_FEED, WDT_FEED_REG);
	
	mutex_unlock(&watchdog.lock);

}

static void watchdog_start(void)
{
	/* Make sure we don't die as soon as the watchdog is enabled below */
	watchdog_keepalive();
	mutex_lock(&watchdog.lock);
	outb(WDT_ENABLE, WDT_ENABLE_REG);
	outb(WDT_RESTART, WDT_PUNCH_REG);
	mutex_unlock(&watchdog.lock);

}

static void watchdog_stop(void)
{
	mutex_lock(&watchdog.lock);
	outb(WDT_DISABLE, WDT_ENABLE_REG);
	mutex_unlock(&watchdog.lock);
}

static int watchdog_get_boot_status(void)
{
	int status = 0;

	mutex_lock(&watchdog.lock);
	status = watchdog.caused_reboot;
	mutex_unlock(&watchdog.lock);

	return status;
}

static bool watchdog_is_running(void)
{
	/*
	 * if we fail to determine the watchdog's status assume it to be
	 * running to be on the safe side
	 */
	bool is_running = true;

	mutex_lock(&watchdog.lock);
	is_running = inb(WDT_ENABLE_REG);
	mutex_unlock(&watchdog.lock);
	
	return is_running;
}

/* /dev/watchdog api */

static int watchdog_open(struct inode *inode, struct file *file)
{
	int err;

	/* If the watchdog is alive we don't need to start it again */
	if (test_and_set_bit(0, &watchdog.opened))
		return -EBUSY;

	watchdog_start();

	if (nowayout)
		__module_get(THIS_MODULE);

	//watchdog.expect_close = 0;
	watchdog.expect_close = 1;
	return nonseekable_open(inode, file);
}

static int watchdog_release(struct inode *inode, struct file *file)
{
	clear_bit(0, &watchdog.opened);

	if (!watchdog.expect_close) {
		watchdog_keepalive();
		pr_crit("Unexpected close, not stopping watchdog!\n");
	} else if (!nowayout) {
		watchdog_stop();
	}
	return 0;
}

/*
 *      watchdog_write:
 *      @file: file handle to the watchdog
 *      @buf: buffer to write
 *      @count: count of bytes
 *      @ppos: pointer to the position to write. No seeks allowed
 *
 *      A write to a watchdog device is defined as a keepalive signal. Any
 *      write of data will do, as we we don't define content meaning.
 */

static ssize_t watchdog_write(struct file *file, const char __user *buf,
			    size_t count, loff_t *ppos)
{
	if (count) {
		if (!nowayout) {
			size_t i;

			/* In case it was set long ago */
			bool expect_close = false;

			for (i = 0; i != count; i++) {
				char c;
				if (get_user(c, buf + i))
					return -EFAULT;
				expect_close = (c == 'V');
			}

			/* Properly order writes across fork()ed processes */
			mutex_lock(&watchdog.lock);
			watchdog.expect_close = expect_close;
			mutex_unlock(&watchdog.lock);
		}

		/* someone wrote to us, we should restart timer */
		watchdog_keepalive();
	}
	return count;
}

/*
 *      watchdog_ioctl:
 *      @inode: inode of the device
 *      @file: file handle to the device
 *      @cmd: watchdog command
 *      @arg: argument pointer
 *
 *      The watchdog API defines a common set of functions for all watchdogs
 *      according to their available features.
 */
static long watchdog_ioctl(struct file *file, unsigned int cmd,
	unsigned long arg)
{
	int status;
	int new_options;
	int new_timeout;
	union {
		struct watchdog_info __user *ident;
		int __user *i;
	} uarg;

	uarg.i = (int __user *)arg;
	
	switch (cmd) {
	case WDIOC_GETSUPPORT:
		return copy_to_user(uarg.ident, &watchdog.ident,
			sizeof(watchdog.ident)) ? -EFAULT : 0;

	case WDIOC_GETSTATUS:
		status = watchdog_is_running();
		return put_user(status, uarg.i);

	case WDIOC_GETBOOTSTATUS:	
		status = watchdog_get_boot_status();
		return put_user(status, uarg.i);

	case WDIOC_SETOPTIONS:
		if (get_user(new_options, uarg.i))
			return -EFAULT;

		if (new_options & WDIOS_DISABLECARD)
			watchdog_stop();
		
		if (new_options & WDIOS_ENABLECARD)
			watchdog_start();
		return 0;

	case WDIOC_KEEPALIVE:
		watchdog_keepalive();
		return 0;

	case WDIOC_SETTIMEOUT:
		if (get_user(new_timeout, uarg.i))
			return -EFAULT;

		if (watchdog_set_timeout(new_timeout))
			return -EINVAL;

		watchdog_keepalive();
		/* Fall */

	case WDIOC_GETTIMEOUT:
		return put_user(watchdog.timeout, uarg.i);

	default:
		return -ENOTTY;

	}
}

static int watchdog_notify_sys(struct notifier_block *this, unsigned long code,
	void *unused)
{
	if (code == SYS_DOWN || code == SYS_HALT)
		watchdog_stop();
	return NOTIFY_DONE;
}

static const struct file_operations watchdog_fops = {
	.owner		= THIS_MODULE,
	.llseek		= no_llseek,
	.open		= watchdog_open,
	.release	= watchdog_release,
	.write		= watchdog_write,
	.unlocked_ioctl	= watchdog_ioctl,
};

static struct miscdevice watchdog_miscdev = {
	//.minor		= WATCHDOG_MINOR,
	.name		= "basecpld_watchdog",
	.fops		= &watchdog_fops,
};

static struct notifier_block watchdog_notifier = {
	.notifier_call = watchdog_notify_sys,
};

static int __init basecpld_wdt_init(void)
{
	int wdt_reboot_cause, err = 0;
	
	watchdog.ident.options = WDIOC_SETTIMEOUT
				| WDIOF_MAGICCLOSE
				| WDIOF_KEEPALIVEPING;

	snprintf(watchdog.ident.identity,
		sizeof(watchdog.ident.identity), "silverstone-x basecpld watchdog");

	wdt_reboot_cause = inb(REBOOT_CAUSE_REG);  // REBOOT_CAUSE
	watchdog.caused_reboot = wdt_reboot_cause;

	err = watchdog_set_timeout(timeout);
	if (err)
		return err;
	
	err = register_reboot_notifier(&watchdog_notifier);
	if (err)
		return err;

	err = misc_register(&watchdog_miscdev);
	if (err) {
		pr_err("cannot register miscdev on minor=%d\n",
		       watchdog_miscdev.minor);
		goto exit_reboot;
	}

	if (timeout) {
		if (timeout <= 0
		 || timeout >  max_timeout) {
			pr_err("starting timeout out of range\n");
			err = -EINVAL;
			goto exit_miscdev;
		}

		watchdog_start();

		mutex_lock(&watchdog.lock);

		if (timeout > MAX_TIMER_VALUE) {    /* 0-200ms 1-30s 2-1min 3-3min 4-4min 5-5min 6-7min 7-10min */
			/* select minutes for timer units */
			outb(MAX_TIMER_VALUE, WDT_TIMER_REG);
		} else {
			/* select seconds for timer units */
			outb(timeout, WDT_TIMER_REG);
		}
		mutex_unlock(&watchdog.lock);

		if (nowayout)
			__module_get(THIS_MODULE);

		pr_info("watchdog started with initial timeout of %u sec\n",
			timeout);
	}

	return 0;

exit_miscdev:
	misc_deregister(&watchdog_miscdev);
exit_reboot:
	unregister_reboot_notifier(&watchdog_notifier);

	return err;
}

static void __exit basecpld_wdt_exit(void)
{
	if (watchdog_is_running()) {
		pr_warn("Watchdog timer still running, stopping it\n");
		watchdog_stop();
	}
	misc_deregister(&watchdog_miscdev);
	unregister_reboot_notifier(&watchdog_notifier);
}

MODULE_DESCRIPTION("basecpld_wdt Watchdog Driver");
MODULE_VERSION("2.0.0");
MODULE_AUTHOR("Nicholas <nicwu@celestica.com>");
MODULE_LICENSE("GPL");

module_init(basecpld_wdt_init);
module_exit(basecpld_wdt_exit);
