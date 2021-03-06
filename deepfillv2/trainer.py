import os
import time
import datetime
import numpy as np
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader

from tensorboardX import SummaryWriter
import torchvision.utils as vutils

import network
import dataset
import utils
import wandb

class Logger:
    def __init__(self, opt):
        wandb.init(project="inpainting")
        wandb.run.name = wandb.run.id
        wandb.run.save()

        self.writer = SummaryWriter()
        self.current_iteration = 0

    def begin(self, n_iter):
        self.current_iteration = n_iter
            
    def add_image(self, image, name='image'):
        """Logs image grid
        
        Args:
            image: [B, 3, H, W] or [B, 1, H, W]
        """
        x = vutils.make_grid(image, normalize=True, scale_each=True)
        self.writer.add_image(name, x, self.current_iteration)
        wandb.log({name: [wandb.Image(x, caption=name)]})
    
    def add_scalars(self, dictionary):
        for k, v in dictionary.items():
            self.writer.add_scalar(k, v, self.current_iteration)
        wandb.log(dictionary)


def create_networks(opt, checkpoint=None):
    generator = utils.create_generator(opt)
    discriminator = utils.create_discriminator(opt)
    perceptualnet = utils.create_perceptualnet()
    
    if checkpoint:
        # Restore the network state
        generator.load_state_dict(checkpoint['G'])
        discriminator.load_state_dict(checkpoint['D'])
    
    # To device
    if opt.multi_gpu == True:
        generator = nn.DataParallel(generator)
        discriminator = nn.DataParallel(discriminator)
        perceptualnet = nn.DataParallel(perceptualnet)
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        perceptualnet = perceptualnet.cuda()
    else:
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        perceptualnet = perceptualnet.cuda()
    
    return generator, discriminator, perceptualnet


def create_optimizers(generator, discriminator, opt, checkpoint=None):
    optimizer_g = torch.optim.Adam(generator.parameters(), lr = opt.lr_g, betas = (opt.b1, opt.b2), weight_decay = opt.weight_decay)
    optimizer_d = torch.optim.Adam(discriminator.parameters(), lr = opt.lr_d, betas = (opt.b1, opt.b2), weight_decay = opt.weight_decay)
    
    def load_optimizer(optimizer, name):
        optimizer.load_state_dict(checkpoint[name])

    if checkpoint:
        load_optimizer(optimizer_g, 'optimizer_g')
        load_optimizer(optimizer_d, 'optimizer_d')
        
    return optimizer_g, optimizer_d


def auto_sync_checkpoints_to_wandb():
    # Save any files starting with "checkpoint" as they're written to
    wandb.save(os.path.join(wandb.run.dir, "checkpoint*"))


# Learning rate decrease
def adjust_learning_rate(lr_in, optimizer, epoch, opt):
    """Set the learning rate to the initial LR decayed by "lr_decrease_factor" every "lr_decrease_epoch" epochs"""
    lr = lr_in * (opt.lr_decrease_factor ** (epoch // opt.lr_decrease_epoch))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def restore(opt):
    if not opt.restore:
        return None
    
    print('-' * 30)
    print(f'Restoring from {opt.wandb_runpath}/{opt.restore}...')
    try:
        if '/home' in opt.wandb_runpath:
            # Restore from local directory
            from shutil import copyfile
            copyfile(opt.wandb_runpath, os.path.join(wandb.run.dir, f'checkpoint_{opt.restore}.pth'))
        else:
            # Copy from a previous run to the current run directory
            wandb.restore(f'checkpoint_{opt.restore}.pth', run_path=opt.wandb_runpath)
        
        # Load the checkpoint
        checkpoint = torch.load(os.path.join(wandb.run.dir, f'checkpoint_{opt.restore}.pth'))
        return checkpoint
    except Exception as e:
        print('Restoring failed :(', e)
        return None
        

def save_state( epoch,batch,n_iter,
                G, optimizer_g,
                D, optimizer_d,
                loss, opt):
    package = lambda model: model.module.state_dict() if opt.multi_gpu else model.state_dict()
    
    state = {
        'epoch': epoch,
        'G': package(G),
        'optimizer_g': package(optimizer_g),
        'D': package(D),
        'optimizer_d': package(optimizer_d),
        'n_iter': n_iter,
        'loss': loss,
    }
    
    print('-' * 30)
    
    path = os.path.join(wandb.run.dir, f'checkpoint_{n_iter}.pth')
    print(f'  Saving at {path}')
    torch.save(state, path)
    
    # also save to latest
    path = os.path.join(wandb.run.dir, f'checkpoint_latest.pth')
    print(f'     You can restore this checkpoint with "--restore latest"')
    torch.save(state, path)
    
    print('-' * 30)
    

def WGAN_trainer(opt):
    # ----------------------------------------
    #      Initialize training parameters
    # ----------------------------------------
    logger = Logger(opt)
    checkpoint = restore(opt)
            
    # cudnn benchmark accelerates the network
    if opt.cudnn_benchmark == True:
        cudnn.benchmark = True
    else:
        cudnn.benchmark = False
    
    # --------------------------------------
    #         Initialize models  
    # --------------------------------------
    generator, discriminator, perceptualnet = create_networks(opt, checkpoint)
    
    # Loss functions
    L1Loss = nn.L1Loss()
    #FeatureMatchingLoss = FML1Loss(opt.fm_param)
    
    # Optimizers
    optimizer_g, optimizer_d = create_optimizers(generator, discriminator, opt, checkpoint)

    # Log metrics with wandb
    wandb.watch(generator)
    wandb.config.update(opt)
    auto_sync_checkpoints_to_wandb()


    # ----------------------------------------
    #       Initialize training dataset
    # ----------------------------------------

    # Define the dataset
    trainset = dataset.InpaintDataset(opt)
    print('The overall number of images equals to %d' % len(trainset))

    # Define the dataloader
    dataloader = DataLoader(trainset, batch_size = opt.batch_size, shuffle = True, num_workers = opt.num_workers, pin_memory = True)
    
    # ----------------------------------------
    #            Training and Testing
    # ----------------------------------------

    # Initialize start time
    prev_time = time.time()

    initial_epoch = checkpoint['epoch'] if opt.restore else 0
    n_iter = checkpoint['n_iter'] if opt.restore else 0
        
    # training loop
    for epoch in range(initial_epoch, opt.epochs):
        for batch_idx, (img, mask) in enumerate(dataloader):
            n_iter += 1
            logger.begin(n_iter)
            
            # Load mask (shape: [B, 1, H, W]), masked_img (shape: [B, 3, H, W]), img (shape: [B, 3, H, W]) and put it to cuda
            img = img.cuda()
            mask = mask.cuda()
            
            ### Train discriminator
            optimizer_d.zero_grad()
            
            # Generator output
            first_out, second_out = generator(img, mask)
            
            # forward propagation
            first_out_wholeimg = img * (1 - mask) + first_out * mask        # in range [-1, 1]
            second_out_wholeimg = img * (1 - mask) + second_out * mask      # in range [-1, 1]
            
            if n_iter % opt.log_every == 1:
                logger.add_image(img, 'image/training')
                logger.add_image(mask, 'mask/training')
                logger.add_image(first_out_wholeimg, 'image/first iteration')
                logger.add_image(second_out_wholeimg, 'image/second iteration')
                
            # Fake samples
            fake_scalar = discriminator(second_out_wholeimg.detach(), mask)
            # True samples
            true_scalar = discriminator(img, mask)
            
            # Overall Loss and optimize
            loss_D = - torch.mean(true_scalar) + torch.mean(fake_scalar)
            loss_D.backward()
            optimizer_d.step()

            ### Train Generator
            optimizer_g.zero_grad()

            # Mask L1 Loss
            first_MaskL1Loss = L1Loss(first_out_wholeimg, img)
            second_MaskL1Loss = L1Loss(second_out_wholeimg, img)
            
            # GAN Loss
            fake_scalar = discriminator(second_out_wholeimg, mask)
            GAN_Loss = - torch.mean(fake_scalar)

            # Get the deep semantic feature maps, and compute Perceptual Loss
            img = (img + 1) / 2                                             # in range [0, 1]
            img = utils.normalize_ImageNet_stats(img)                       # in range of ImageNet
            img_featuremaps = perceptualnet(img)                            # feature maps
            second_out_wholeimg = (second_out_wholeimg + 1) / 2             # in range [0, 1]
            second_out_wholeimg = utils.normalize_ImageNet_stats(second_out_wholeimg)
            second_out_wholeimg_featuremaps = perceptualnet(second_out_wholeimg)
            second_PerceptualLoss = L1Loss(second_out_wholeimg_featuremaps, img_featuremaps)

            # Compute losses
            loss = first_MaskL1Loss + second_MaskL1Loss + opt.perceptual_param * second_PerceptualLoss + opt.gan_param * GAN_Loss
            loss.backward()
            optimizer_g.step()

            # Determine approximate time left
            batches_done = n_iter
            batches_left = opt.epochs * len(dataloader) - batches_done
            time_left = datetime.timedelta(seconds = batches_left * (time.time() - prev_time))
            prev_time = time.time()
            
            logger.add_scalars({
                'Epoch': epoch + 1,
                'Iteration': n_iter,
                'loss/first Mask L1 Loss': first_MaskL1Loss.item(),
                'loss/second Mask L1 Loss': second_MaskL1Loss.item(),
                'gan/D Loss': loss_D.item(),
                'gan/G Loss': GAN_Loss.item(),
                'Perceptual Loss': second_PerceptualLoss.item()
            })
            
            # Print log
            if n_iter % opt.log_every == 1:
                print("\r[Epoch %d/%d] [Batch %d/%d] iteration %d" %
                    ((epoch + 1), opt.epochs, batch_idx, len(dataloader), n_iter))
                print("\r[D Loss: %.5f] [G Loss: %.5f] [Perceptual Loss: %.5f] time_left: %s" %
                    (loss_D.item(), GAN_Loss.item(), second_PerceptualLoss.item(), time_left))
            
            if n_iter % opt.checkpoint_every == 1:
                save_state(
                    epoch=epoch,
                    batch=batch_idx,
                    n_iter=n_iter,
                    G=generator,
                    optimizer_g=optimizer_g,
                    D=discriminator,
                    optimizer_d=optimizer_d,
                    loss=loss,
                    opt=opt
                )

        # Learning rate decrease
        adjust_learning_rate(opt.lr_g, optimizer_g, (epoch + 1), opt)
        adjust_learning_rate(opt.lr_d, optimizer_d, (epoch + 1), opt)


def LSGAN_trainer(opt):
    # ----------------------------------------
    #      Initialize training parameters
    # ----------------------------------------

    # cudnn benchmark accelerates the network
    if opt.cudnn_benchmark == True:
        cudnn.benchmark = True
    else:
        cudnn.benchmark = False

    # Build networks
    generator = utils.create_generator(opt)
    discriminator = utils.create_discriminator(opt)
    perceptualnet = utils.create_perceptualnet()

    # To device
    if opt.multi_gpu == True:
        generator = nn.DataParallel(generator)
        discriminator = nn.DataParallel(discriminator)
        perceptualnet = nn.DataParallel(perceptualnet)
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        perceptualnet = perceptualnet.cuda()
    else:
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        perceptualnet = perceptualnet.cuda()

    # Loss functions
    L1Loss = nn.L1Loss()
    MSELoss = nn.MSELoss()
    #FeatureMatchingLoss = FML1Loss(opt.fm_param)

    # Optimizers
    optimizer_g = torch.optim.Adam(generator.parameters(), lr = opt.lr_g, betas = (opt.b1, opt.b2), weight_decay = opt.weight_decay)
    optimizer_d = torch.optim.Adam(generator.parameters(), lr = opt.lr_d, betas = (opt.b1, opt.b2), weight_decay = opt.weight_decay)

    # Learning rate decrease
    def adjust_learning_rate(lr_in, optimizer, epoch, opt):
        """Set the learning rate to the initial LR decayed by "lr_decrease_factor" every "lr_decrease_epoch" epochs"""
        lr = lr_in * (opt.lr_decrease_factor ** (epoch // opt.lr_decrease_epoch))
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
    
    # Save the model if pre_train == True
    def save_model(net, epoch, opt):
        """Save the model at "checkpoint_interval" and its multiple"""
        if opt.multi_gpu == True:
            if epoch % opt.checkpoint_interval == 0:
                torch.save(net.module, 'deepfillNet_epoch%d_batchsize%d.pth' % (epoch, opt.batch_size))
                print('The trained model is successfully saved at epoch %d' % (epoch))
        else:
            if epoch % opt.checkpoint_interval == 0:
                torch.save(net, 'deepfillNet_epoch%d_batchsize%d.pth' % (epoch, opt.batch_size))
                print('The trained model is successfully saved at epoch %d' % (epoch))
    
    # ----------------------------------------
    #       Initialize training dataset
    # ----------------------------------------

    # Define the dataset
    trainset = dataset.InpaintDataset(opt)
    print('The overall number of images equals to %d' % len(trainset))

    # Define the dataloader
    dataloader = DataLoader(trainset, batch_size = opt.batch_size, shuffle = True, num_workers = opt.num_workers, pin_memory = True)
    
    # ----------------------------------------
    #            Training and Testing
    # ----------------------------------------

    # Initialize start time
    prev_time = time.time()
    
    # Tensor type
    Tensor = torch.cuda.FloatTensor

    # Training loop
    for epoch in range(opt.epochs):
        for batch_idx, (img, mask) in enumerate(dataloader):

            # Load mask (shape: [B, 1, H, W]), masked_img (shape: [B, 3, H, W]), img (shape: [B, 3, H, W]) and put it to cuda
            img = img.cuda()
            mask = mask.cuda()

            # LSGAN vectors
            valid = Tensor(np.ones((img.shape[0], 1, 8, 8)))
            fake = Tensor(np.zeros((img.shape[0], 1, 8, 8)))

            ### Train Discriminator
            optimizer_d.zero_grad()

            # Generator output
            first_out, second_out = generator(img, mask)

            # forward propagation
            first_out_wholeimg = img * (1 - mask) + first_out * mask        # in range [-1, 1]
            second_out_wholeimg = img * (1 - mask) + second_out * mask      # in range [-1, 1]

            # Fake samples
            fake_scalar = discriminator(second_out_wholeimg.detach(), mask)
            # True samples
            true_scalar = discriminator(img, mask)
            
            # Overall Loss and optimize
            loss_fake = MSELoss(fake_scalar, fake)
            loss_true = MSELoss(true_scalar, valid)
            # Overall Loss and optimize
            loss_D = 0.5 * (loss_fake + loss_true)
            loss_D.backward()
            optimizer_d.step()

            ### Train Generator
            optimizer_g.zero_grad()

            # Mask L1 Loss
            first_MaskL1Loss = L1Loss(first_out_wholeimg, img)
            second_MaskL1Loss = L1Loss(second_out_wholeimg, img)
            
            # GAN Loss
            fake_scalar = discriminator(second_out_wholeimg, mask)
            GAN_Loss = MSELoss(fake_scalar, valid)

            # Get the deep semantic feature maps, and compute Perceptual Loss
            img = (img + 1) / 2                                             # in range [0, 1]
            img = utils.normalize_ImageNet_stats(img)                       # in range of ImageNet
            img_featuremaps = perceptualnet(img)                            # feature maps
            second_out_wholeimg = (second_out_wholeimg + 1) / 2             # in range [0, 1]
            second_out_wholeimg = utils.normalize_ImageNet_stats(second_out_wholeimg)
            second_out_wholeimg_featuremaps = perceptualnet(second_out_wholeimg)
            second_PerceptualLoss = L1Loss(second_out_wholeimg_featuremaps, img_featuremaps)

            # Compute losses
            loss = first_MaskL1Loss + second_MaskL1Loss + opt.perceptual_param * second_PerceptualLoss + opt.gan_param * GAN_Loss
            loss.backward()
            optimizer_g.step()

            # Determine approximate time left
            batches_done = epoch * len(dataloader) + batch_idx
            batches_left = opt.epochs * len(dataloader) - batches_done
            time_left = datetime.timedelta(seconds = batches_left * (time.time() - prev_time))
            prev_time = time.time()

            # Print log
            print("\r[Epoch %d/%d] [Batch %d/%d] [first Mask L1 Loss: %.5f] [second Mask L1 Loss: %.5f]" %
                ((epoch + 1), opt.epochs, batch_idx, len(dataloader), first_MaskL1Loss.item(), second_MaskL1Loss.item()))
            print("\r[D Loss: %.5f] [G Loss: %.5f] [Perceptual Loss: %.5f] time_left: %s" %
                (loss_D.item(), GAN_Loss.item(), second_PerceptualLoss.item(), time_left))

        # Learning rate decrease
        adjust_learning_rate(opt.lr_g, optimizer_g, (epoch + 1), opt)
        adjust_learning_rate(opt.lr_d, optimizer_d, (epoch + 1), opt)

        # Save the model
        save_model(generator, (epoch + 1), opt)
